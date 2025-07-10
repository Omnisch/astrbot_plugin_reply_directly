import asyncio
import json
from typing import Set

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

# 用于防止主动插话功能在短时间内对同一个会话重复触发
# 存储结构: { "unified_msg_origin": asyncio.Lock() }
proactive_locks = {}

@register(
    "reply_directly",
    "qa296",
    "一个实现沉浸式对话和主动插话的插件",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly",
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 用于存储被LLM指定需要进行一次主动回复的用户
        # 存储 unified_msg_origin 字符串
        self.sticky_reply_targets: Set[str] = set()
        logger.info("主动回复插件加载成功。")

    # --- 功能1: 沉浸式对话 (Sticky Reply) ---

    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        当与用户对话非常流畅或进入深入话题时调用此工具。
        调用后，机器人下一次将主动回复该用户，无需等待@。这会创建更自然的对话流。
        """
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return

        uid = event.unified_msg_origin
        if uid:
            self.sticky_reply_targets.add(uid)
            logger.info(f"已为会话 {uid} 开启一次性沉浸式对话。")
            # 给用户一个正向反馈
            yield event.plain_result("好的，我记住了，下次我会注意听你讲。")
        else:
            yield event.plain_result("无法确定当前会话。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def sticky_reply_handler(self, event: AstrMessageEvent):
        """
        监听所有消息，处理沉浸式对话的触发
        """
        # 检查总开关和功能分开关
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return
        
        # 如果是@机器人或者私聊，则正常流程处理，此handler不拦截
        if event.is_at_or_wake_command:
            return

        uid = event.unified_msg_origin
        if uid in self.sticky_reply_targets:
            logger.info(f"沉浸式对话触发，主动回复: {uid}")
            # 使用后立即移除，确保只生效一次
            self.sticky_reply_targets.remove(uid)

            # 让LLM处理这条消息并回复
            # 使用 event.request_llm 会经过完整的 LLM 处理流程
            yield event.request_llm(prompt=event.get_message_str())
            
            # 停止事件传播，防止其他插件或默认的LLM回复再次响应
            event.stop_event()

    # --- 功能2: 主动插话 (Proactive Reply) ---

    @filter.after_message_sent()
    async def on_after_bot_sent_message(self, event: AstrMessageEvent):
        """
        当机器人发送消息后触发此钩子
        """
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return

        # 获取锁，如果锁不存在则创建一个
        lock = proactive_locks.setdefault(event.unified_msg_origin, asyncio.Lock())
        if lock.locked():
            logger.debug(f"会话 {event.unified_msg_origin} 的主动插话任务已在运行，本次跳过。")
            return

        # 创建一个异步任务来执行后续的监听和判断
        asyncio.create_task(self.proactive_reply_task(event, lock))

    async def proactive_reply_task(self, event: AstrMessageEvent, lock: asyncio.Lock):
        """
        实际执行主动插话逻辑的异步任务
        """
        uid = event.unified_msg_origin
        async with lock:
            try:
                # 从配置中读取延迟时间
                delay = self.config.get("proactive_reply", {}).get("delay_seconds", 5)
                logger.info(f"会话 {uid} 进入主动插话观察期，时长: {delay}秒")
                await asyncio.sleep(delay)

                # 延迟后，获取最新的对话历史
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
                if not curr_cid:
                    logger.warning(f"无法为会话 {uid} 找到当前对话ID，无法执行主动插话。")
                    return
                
                conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
                if not conversation or not conversation.history:
                    return

                history = json.loads(conversation.history)
                
                # 获取配置的历史消息数量限制
                limit = self.config.get("proactive_reply", {}).get("history_limit", 10)
                recent_history = history[-limit:]

                # 如果最近一条消息是机器人自己发的，就不插话，避免自我循环
                if recent_history and recent_history[-1].get("role") != "user":
                    logger.info(f"会话 {uid} 最近一条消息非用户消息，取消主动插话。")
                    return
                
                # 构造给LLM的请求
                prompt = self._build_proactive_prompt(recent_history)
                system_prompt = (
                    "你是一个聪明的聊天机器人，正在观察群聊。请根据以下最近的聊天记录，判断你是否应该主动插话参与讨论。"
                    "你的回答必须是一个JSON对象，格式如下：\n"
                    '{"should_reply": boolean, "reply_content": "string"}\n'
                    "如果should_reply为true，请在reply_content中提供一句自然、简短且切题的回复。如果为false，reply_content应为空字符串。"
                )

                logger.info(f"会话 {uid} 正在请求LLM判断是否需要主动插话。")
                
                # 使用底层的 text_chat，因为它允许我们自己处理返回结果
                llm_response = await self.context.get_using_provider().text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    contexts=[] # 上下文已在prompt中处理，此处为空
                )

                if not llm_response or not llm_response.completion_text:
                    return

                decision = self._parse_llm_decision(llm_response.completion_text)

                if decision and decision.get("should_reply"):
                    reply_content = decision.get("reply_content")
                    if reply_content:
                        logger.info(f"LLM决定在会话 {uid} 中主动插话，内容: {reply_content}")
                        # 使用 context.send_message 主动发送消息
                        await self.context.send_message(uid, reply_content)

            except Exception as e:
                logger.error(f"主动插话任务失败: {e}", exc_info=True)
    
    def _build_proactive_prompt(self, history: list) -> str:
        """
        将历史记录格式化为清晰的文本块
        """
        formatted_lines = ["--- 最近聊天记录开始 ---"]
        for msg in history:
            role = "用户" if msg.get("role") == "user" else "你"
            content = msg.get("content", "")
            if isinstance(content, list): # 处理图文消息
                text_parts = [part['text'] for part in content if part.get('type') == 'text']
                content = " ".join(text_parts) if text_parts else "[非文本消息]"
            formatted_lines.append(f"{role}: {content}")
        formatted_lines.append("--- 最近聊天记录结束 ---")
        formatted_lines.append("\n请判断我是否应该插话。")
        return "\n".join(formatted_lines)

    def _parse_llm_decision(self, text: str) -> dict:
        """
        安全地解析LLM返回的JSON字符串
        """
        try:
            # 尝试从文本中提取JSON块
            start_index = text.find('{')
            end_index = text.rfind('}') + 1
            if start_index != -1 and end_index != -1:
                json_str = text[start_index:end_index]
                return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"解析LLM的插话决策失败: {e}. 原始文本: {text}")
        return None

    async def terminate(self):
        """
        插件卸载/停用时调用，进行清理
        """
        self.sticky_reply_targets.clear()
        # 清理可能存在的锁
        for lock in proactive_locks.values():
            if lock.locked():
                # 无法直接"解锁"一个不属于当前任务的锁，但清空引用有助于垃圾回收
                pass
        proactive_locks.clear()
        logger.info("主动回复插件已停用并清理资源。")
