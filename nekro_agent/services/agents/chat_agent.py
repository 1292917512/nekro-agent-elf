import os
import time
from typing import List, Optional, Union

from miose_toolkit_llm import (
    BaseScene,
    BaseStore,
    ModelResponse,
    Runner,
)
from miose_toolkit_llm.clients.chat_openai import (
    OpenAIChatClient,
)
from miose_toolkit_llm.components import (
    TextComponent,
)
from miose_toolkit_llm.creators.openai import (
    AiMessage,
    OpenAIPromptCreator,
    SystemMessage,
    UserMessage,
)
from miose_toolkit_llm.exceptions import (
    ResolveError,
    SceneRuntimeError,
)
from miose_toolkit_llm.tools.tokenizers import TikTokenizer

from nekro_agent.core import logger
from nekro_agent.core.config import config
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.chat import chat_service
from nekro_agent.services.sandbox.executor import CODE_RUN_ERROR_FLAG, limited_run_code

from .components.chat_history_cmp import ChatHistoryComponent
from .components.chat_ret_cmp import ChatResponseResolver, ChatResponseType

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


class ChatScene(BaseScene):
    """基本对话场景类"""

    class Store(BaseStore):
        """场景数据源类"""

        chat_key: str = ""
        chat_preset: str = config.AI_CHAT_PRESET_SETTING
        one_time_code: str = ""


async def agent_run(
    chat_message: ChatMessage,
    addition_prompt_message: Optional[List[Union[UserMessage, AiMessage]]] = None,
    retry_depth: int = 0,
):
    """代理执行函数"""

    sta_timestamp = time.time()
    one_time_code = os.urandom(4).hex()  # 防止提示词注入，生成一次性随机码

    if not addition_prompt_message:
        addition_prompt_message = []

    logger.info(f"正在构建对话场景: {chat_message.chat_key}")
    if config.DEBUG_IN_CHAT:
        await chat_service.send_message(chat_message.chat_key, "[Debug] 思考中🤔...")
    # 1. 构造一个应用场景
    scene = ChatScene()
    scene.store.set("chat_key", chat_message.chat_key)
    scene.store.set("one_time_code", one_time_code)

    # 2. 构建聊天记录组件
    chat_history_component = ChatHistoryComponent(scene).bind(
        param_key="one_time_code",
        store_key="one_time_code",
        src_store=scene.store,
    )
    sta_timestamp = int(time.time() - config.AI_CHAT_CONTEXT_EXPIRE_SECONDS)
    recent_chat_messages: List[DBChatMessage] = (
        DBChatMessage.sqa_query()
        .filter(
            DBChatMessage.chat_key == chat_message.chat_key,
            DBChatMessage.send_timestamp >= sta_timestamp,
        )
        .order_by(DBChatMessage.send_timestamp.desc())
        .limit(config.AI_CHAT_CONTEXT_MAX_LENGTH)
        .all()
    )[::-1][-config.AI_CHAT_CONTEXT_MAX_LENGTH :]
    for db_message in recent_chat_messages:
        chat_history_component.append_chat_message(db_message)

    # 3. 构造 OpenAI 提示词
    prompt_creator = OpenAIPromptCreator(
        SystemMessage(
            TextComponent(
                "Character Stetting For You: {chat_preset}",
                src_store=scene.store,
            ),
            ChatResponseResolver.example(one_time_code),  # 生成一个解析结果示例
            sep="\n\n",  # 自定义构建 prompt 的分隔符 默认为 "\n"
        ),
        UserMessage(
            TextComponent(
                "Current Chat Key: {chat_key}",
                src_store=scene.store,
            ),
            chat_history_component,
            "Please refer to the above information, strictly follow the reply requirements, and do not bring any irrelevant information。",
        ),
        *addition_prompt_message,
        # 生成使用的参数
        temperature=0.3,
        presence_penalty=0.3,
        frequency_penalty=0.4,
    )

    # 4. 绑定 LLM 执行器
    scene.attach_runner(  # 为场景绑定 LLM 执行器
        Runner(
            client=OpenAIChatClient(
                model=config.CHAT_MODEL,
                api_key=config.OPENAI_API_KEY or OPENAI_API_KEY,
                base_url=config.OPENAI_BASE_URL or OPENAI_BASE_URL,
            ),  # 指定聊天客户端
            tokenizer=TikTokenizer(model=config.CHAT_MODEL),  # 指定分词器
            prompt_creator=prompt_creator,
        ),
    )

    # 5. 获取结果与解析
    for _ in range(config.AI_CHAT_LLM_API_MAX_RETRIES):

        try:
            mr: ModelResponse = await scene.run()
            break
        except Exception as e:
            logger.error(f"LLM API error: {e}")
    else:
        await chat_service.send_agent_message(chat_message.chat_key, "哎呀，请求模型发生了未知错误，等会儿再试试吧 ~")
        raise SceneRuntimeError("LLM API error: 达到最大重试次数，停止重试。")

    try:
        resolved_response: ChatResponseResolver = ChatResponseResolver.resolve(
            model_response=mr,
        )  # 使用指定解析器解析结果
    except Exception as e:
        logger.error(f"解析结果出错: {e}")
        raise ResolveError(f"解析结果出错: {e}") from e

    # 6. 反馈与保存数据
    mr.save(
        prompt_file=".temp/chat_prompt-latest.txt",
        response_file=".temp/chat_response-latest.json",
    )
    mr.save(
        prompt_file=f".temp/prompts/chat_prompt-{time.strftime('%Y%m%d%H%M%S')}.txt",
        response_file=f".temp/prompts/chat_response-{time.strftime('%Y%m%d%H%M%S')}.json",
    )

    # 7. 执行响应结果
    for ret_data in resolved_response.ret_list:
        await agent_exec_result(ret_data.type, ret_data.content, chat_message, addition_prompt_message, retry_depth)
    
    logger.info(f"本轮响应耗时: {time.time() - sta_timestamp:.2f}s | To {chat_message.sender_nickname}")


async def agent_exec_result(
    ret_type: ChatResponseType,
    ret_content: str,
    chat_message: ChatMessage,
    addition_prompt_message: List[Union[UserMessage, AiMessage]],
    retry_depth: int = 0,
):
    if ret_type is ChatResponseType.TEXT:
        logger.info(f"解析文本回复: {ret_content} | To {chat_message.sender_nickname}")
        await chat_service.send_agent_message(chat_message.chat_key, ret_content, record=True)
        return

    if ret_type is ChatResponseType.SCRIPT:
        logger.info(f"解析程式回复: 等待执行资源 | To {chat_message.sender_nickname}")
        if config.DEBUG_IN_CHAT:
            await chat_service.send_message(chat_message.chat_key, "[Debug] 执行程式中🖥️...")
        result: str = await limited_run_code(ret_content, from_chat_key=chat_message.chat_key)
        if result.endswith(CODE_RUN_ERROR_FLAG):  # 运行出错标记，将错误信息返回给 AI
            err_msg = result[: -len(CODE_RUN_ERROR_FLAG)]
            addition_prompt_message.append(AiMessage(f"script:>\n{ret_content}"))
            if retry_depth < config.AI_SCRIPT_MAX_RETRY_TIMES - 1:
                addition_prompt_message.append(
                    UserMessage(
                        f"Code run error: {err_msg or 'No error message'}\nPlease maintain agreed reply format and try again.",
                    ),
                )
            else:
                addition_prompt_message.append(
                    UserMessage(
                        f"Code run error: {err_msg or 'No error message'}\nThe number of retries has reached the limit, you should give up retries and explain the problem you are experiencing.",
                    ),
                )
            logger.info(f"程式运行出错: ...{err_msg[-100:]} | 重试次数: {retry_depth} | To {chat_message.sender_nickname}")
            if retry_depth < config.AI_SCRIPT_MAX_RETRY_TIMES:
                if config.DEBUG_IN_CHAT:
                    await chat_service.send_message(
                        chat_message.chat_key,
                        f"[Debug] 程式运行出错: {err_msg or 'No error message'}\n正在调试中...({retry_depth + 1}/{config.AI_SCRIPT_MAX_RETRY_TIMES})",
                    )
                await agent_run(chat_message, addition_prompt_message, retry_depth + 1)
            else:
                await chat_service.send_message(chat_message.chat_key, "程式运行出错，达到最大重试次数，停止重试。")
        return
