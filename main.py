import asyncio
import json
from collections import defaultdict, deque
from typing import Set, Dict, Deque, Tuple, Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# 用于存储被标记为“沉浸式对话”的会话ID
# 使用 set 是为了快速查找和删除
sticky_sessions: Set[str] = set()

# 用于存储主动插话的异步任务，防止重复触发
# key: unified_msg_origin, value: asyncio.Task
proactive_tasks: Dict[str, asyncio.Task] = {}

# 用于存储每个会话的近期聊天记录
# key: unified_msg_origin, value: deque of (sender_name, message_str)
chat_history: Dict[str, Deque[Tuple[str, str]]] = defaultdict(lambda: deque(maxlen=20))


@register(
    "astrbot_plugin_reply_directly",
    "qa296",  
    "实现沉浸式对话（无需@主动回复一次）和主动插话功能。",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly" 
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info("DirectReply 插件已加载。")

    # --- 功能1: 沉浸式对话 ---

    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply(self, event: AstrMessageEvent):
        """
        当您认为与用户的对话非常流畅，并希望在下一次无需用户@您时主动回复时，可以调用此函数。此功能仅生效一次。

        Args:
            None
        """
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return event.plain_result("沉浸式对话功能未开启。")
        
        origin = event.unified_msg_origin
        sticky_sessions.add(origin)
        logger.info(f"[沉浸式对话] 已为会话 {origin} 开启一次性主动回复。")


    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def handle_sticky_reply(self, event: AstrMessageEvent):
        """
        处理所有消息，检查是否来自被标记的“沉浸式”会话。
        高优先级(priority=1)确保它在默认LLM处理之前运行。
        """
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return

        origin = event.unified_msg_origin
        # 如果会话在我们的集合中，并且这次消息没有@机器人
        if origin in sticky_sessions and not event.is_wake_up():
            logger.info(f"[沉浸式对话] 触发对 {origin} 的主动回复。")
            # 用完一次就移除
            sticky_sessions.remove(origin)
            
            # 阻止后续的默认LLM调用，因为我们在这里手动调用
            event.should_call_llm(False)
            # 停止事件继续传播，防止其他插件处理
            event.stop_event()

            # 手动请求LLM处理这条消息
            yield event.request_llm(prompt=event.get_message_str())


    # --- 功能2: 主动插话 ---

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def record_chat_history(self, event: AstrMessageEvent):
        """
        低优先级监听所有消息，用于记录聊天历史和取消正在计时的插话任务。
        """
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return

        origin = event.unified_msg_origin
        
        # 记录消息
        sender_name = event.get_sender_name() or event.get_sender_id()
        message_text = event.get_message_str()
        if message_text:
            # 更新deque的最大长度以匹配配置
            proactive_config = self.config.get("proactive_reply", {})
            history_limit = proactive_config.get("history_limit", 10)
            if chat_history[origin].maxlen != history_limit:
                 chat_history[origin] = deque(chat_history[origin], maxlen=history_limit)

            chat_history[origin].append((sender_name, message_text))

        # 如果有新的聊天消息，就取消之前计划的“主动插话”任务，因为对话正在进行
        if origin in proactive_tasks and not proactive_tasks[origin].done():
            logger.debug(f"[主动插话] 会话 {origin} 有新消息，取消计时。")
            proactive_tasks[origin].cancel()
            del proactive_tasks[origin]


    @filter.after_message_sent()
    async def schedule_proactive_check(self, event: AstrMessageEvent):
        """
        在机器人发送消息后触发，启动一个异步任务来检查是否需要主动插话。
        """
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return
        
        # 确保事件是由机器人自己发送消息触发的
        if event.get_sender_id() != event.get_self_id():
            return

        origin = event.unified_msg_origin
        
        # 如果已有任务，先取消
        if origin in proactive_tasks and not proactive_tasks[origin].done():
            proactive_tasks[origin].cancel()

        # 创建新的计时任务
        logger.debug(f"[主动插话] 机器人已发言，为会话 {origin} 启动插话检查计时。")
        task = asyncio.create_task(self._proactive_check(origin, event.get_sender_name()))
        proactive_tasks[origin] = task


    async def _proactive_check(self, origin: str, bot_name: str):
        """
        异步检查函数，在延迟后执行。
        """
        proactive_config = self.config.get("proactive_reply", {})
        delay = proactive_config.get("delay_seconds", 5)

        try:
            await asyncio.sleep(delay)
            
            logger.info(f"[主动插话] 检查会话 {origin} 是否需要插话。")

            history = list(chat_history.get(origin, []))
            if not history:
                logger.debug(f"[主动插话] {origin} 无历史记录，不插话。")
                return

            # 找到机器人最后一次说话的位置
            last_bot_msg_index = -1
            for i in range(len(history) - 1, -1, -1):
                if history[i][0] == bot_name:
                    last_bot_msg_index = i
                    break
            
            # 获取机器人说话之后的新消息
            new_messages = history[last_bot_msg_index + 1:]

            if not new_messages:
                logger.info(f"[主动插话] {origin} 在机器人发言后无新消息，不插话。")
                return
                
            # 格式化新消息给LLM
            formatted_history = "\n".join([f"{name}: {msg}" for name, msg in new_messages])
            
            system_prompt = (
                "你是一个聊天群的观察者。请分析以下在几秒钟内发生的对话片段。\n"
                "你的任务是判断，作为一个AI助手，此时主动插话是否自然且有帮助。\n"
                "如果对话已经结束、话题不适合你介入、或你认为保持沉默更好，你必须仅返回一个JSON对象：{\"should_reply\": false, \"reply_content\": \"\"}。\n"
                "如果你认为你应该回复，请返回JSON对象：{\"should_reply\": true, \"reply_content\": \"你的回复内容\"}。\n"
                "你的整个回答必须是一个严格符合此格式的JSON对象，不要添加任何额外的解释或文字。"
            )
            
            prompt = f"这是最近的对话：\n---\n{formatted_history}\n---\n根据以上内容，请做出你的判断。"
            
            logger.debug(f"[主动插话] 发送给LLM的提示词: {prompt}")

            # 直接调用LLM provider
            llm_response: Optional[LLMResponse] = await self.context.get_using_provider().text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                contexts=[] # 我们只关心当前片段，不使用历史上下文
            )

            if not llm_response or not llm_response.completion_text:
                logger.warning("[主动插话] LLM没有返回有效内容。")
                return
            
            try:
                decision = json.loads(llm_response.completion_text)
                should_reply = decision.get("should_reply", False)
                reply_content = decision.get("reply_content", "")

                if should_reply and reply_content:
                    logger.info(f"[主动插话] LLM决定插话，内容: {reply_content}")
                    message_chain = [Plain(text=reply_content)]
                    # 使用 context.send_message 主动发送消息
                    await self.context.send_message(origin, message_chain)
                else:
                    logger.info("[主动插话] LLM决定不插话。")
                    
            except (json.JSONDecodeError, TypeError) as e:
                logger.error(f"[主动插话] LLM返回的不是有效的JSON格式: {llm_response.completion_text} | 错误: {e}")

        except asyncio.CancelledError:
            logger.debug(f"[主动插话] 会话 {origin} 的检查任务被取消。")
        except Exception as e:
            logger.error(f"[主动插话] 检查过程中发生未知错误: {e}", exc_info=True)
        finally:
            # 任务结束或被取消后，从字典中移除
            if origin in proactive_tasks:
                del proactive_tasks[origin]

    async def terminate(self):
        """
        插件卸载或停用时调用，清理资源。
        """
        logger.info("DirectReply 插件正在卸载...")
        # 取消所有正在运行的计时任务
        for task in proactive_tasks.values():
            if not task.done():
                task.cancel()
        proactive_tasks.clear()
        sticky_sessions.clear()
        chat_history.clear()
        logger.info("DirectReply 插件资源已清理。")
