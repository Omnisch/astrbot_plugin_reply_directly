import asyncio
import json
from collections import defaultdict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import MessageEventResult

# 使用 defaultdict 来简化消息缓冲区的初始化
# 它会在访问一个不存在的 key 时，自动创建一个空的 list
# 格式: { "group_id": ["user1: msg1", "user2: msg2"] }
proactive_message_buffers = defaultdict(list)

@register(
    "reply_directly",
    "qa296",
    "实现沉浸式对话和主动插话功能，提升群聊体验。",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 用于存储需要“沉浸式回复”的群组ID
        self.sticky_reply_groups = set()
        # 用于管理“主动插话”的计时器任务，方便取消
        # 格式: { "group_id": asyncio.Task }
        self.proactive_timers = {}
        logger.info("ReplyDirectlyPlugin 已加载。")

    # 1. 沉浸式对话功能
    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        让机器人准备好在下一次群聊消息时直接回复，无需被@。此效果仅生效一次。
        当LLM认为和用户的对话很投机，希望下一次能主动回应时，可以调用此工具。
        """
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return

        group_id = event.get_group_id()
        if group_id:
            self.sticky_reply_groups.add(group_id)
            logger.info(f"[沉浸式对话] 已为群组 {group_id} 启用一次性直接回复。")
        
        # 这个函数工具是静默的，不产生任何聊天消息
        # 因此我们不 yield任何结果

    # 监听所有群聊消息，处理沉浸式对话和为主动插话收集信息
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin"):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        # 检查是否需要“沉浸式回复”
        if self.config.get("sticky_reply", {}).get("enable") and group_id in self.sticky_reply_groups:
            logger.info(f"[沉浸式对话] 触发群组 {group_id} 的直接回复。")
            self.sticky_reply_groups.remove(group_id)  # 移除，确保只生效一次

            # 将当前消息作为对LLM的请求，并停止事件继续传播
            yield event.request_llm(prompt=event.message_str, image_urls=event.get_image_urls())
            event.stop_event()
            return

        # 如果不满足沉浸式回复，则为“主动插话”功能收集消息
        if self.config.get("proactive_reply", {}).get("enable"):
            # 检查是否有正在计时的任务，如果有，说明机器人刚说完话，现在是收集时间
            if group_id in self.proactive_timers:
                user_name = event.get_sender_name()
                message_text = event.message_str
                formatted_message = f"{user_name}: {message_text}"
                proactive_message_buffers[group_id].append(formatted_message)
                logger.debug(f"[主动插话] 收集到群 {group_id} 的消息: {formatted_message}")

    # 2. 主动插话功能
    # 监听机器人自己发送的消息
    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return

        group_id = event.get_group_id()
        # 只处理群聊消息
        if not group_id:
            return

        # 如果该群已有计时器，先取消旧的
        if group_id in self.proactive_timers:
            self.proactive_timers[group_id].cancel()
            logger.debug(f"[主动插话] 取消了群 {group_id} 的旧计时器。")

        # 清空该群之前的消息缓冲区
        proactive_message_buffers[group_id].clear()

        # 创建一个新的计时器任务
        delay = self.config.get("proactive_reply", {}).get("delay_seconds", 5)
        task = asyncio.create_task(self._proactive_check_and_reply(event, group_id, delay))
        self.proactive_timers[group_id] = task
        logger.info(f"[主动插话] 已为群 {group_id} 启动 {delay} 秒的插话计时器。")

    async def _proactive_check_and_reply(self, event: AstrMessageEvent, group_id: str, delay: int):
        try:
            await asyncio.sleep(delay)

            # 从缓冲区获取收集到的消息
            messages = proactive_message_buffers[group_id]
            if not messages:
                logger.info(f"[主动插话] 群 {group_id} 在 {delay} 秒内无新消息，不执行操作。")
                return

            history_limit = self.config.get("proactive_reply", {}).get("history_limit", 10)
            # 限制分析的消息数量
            recent_messages = messages[-history_limit:]
            conversation_snippet = "\n".join(recent_messages)
            
            logger.info(f"[主动插话] 群 {group_id} 计时结束，准备分析以下内容:\n{conversation_snippet}")

            # 构建特殊的prompt
            prompt = (
                "你是一个在群聊中的AI助手。下面是你发言后一小段时间内群里的聊天内容。"
                "请判断你是否应该根据这些内容主动插话。如果不需要回复，或者内容与你无关，请回答 '{\"should_reply\": false, \"content\": \"\"}'。"
                "如果需要回复，请在 'content' 字段中提供你的回复内容，并确保 'should_reply' 为 true。\n"
                "你的回答必须是严格的JSON格式。\n\n"
                f"聊天内容：\n---\n{conversation_snippet}\n---\n"
            )

            # 使用底层API调用LLM，避免触发其他钩子
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                return

            llm_response = await provider.text_chat(prompt=prompt)

            # 解析LLM的JSON响应
            try:
                response_json = json.loads(llm_response.completion_text)
                should_reply = response_json.get("should_reply", False)
                content = response_json.get("content", "")

                if should_reply and content:
                    logger.info(f"[主动插话] LLM决定在群 {group_id} 插话，内容: {content}")
                    # 使用 context.send_message 主动发送消息
                    await self.context.send_message(event.unified_msg_origin, content)
                else:
                    logger.info(f"[主动插话] LLM决定不在群 {group_id} 插话。")

            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.error(f"[主动插话] 解析LLM响应失败: {e}\n原始响应: {llm_response.completion_text}")

        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的计时器被取消。")
        except Exception as e:
            logger.error(f"[主动插话] 执行检查时发生未知错误: {e}")
        finally:
            # 任务结束或被取消后，清理资源
            if group_id in self.proactive_timers:
                del self.proactive_timers[group_id]
            if group_id in proactive_message_buffers:
                proactive_message_buffers[group_id].clear()

    async def terminate(self):
        """插件卸载/停用时，取消所有正在运行的计时器任务"""
        for task in self.proactive_timers.values():
            task.cancel()
        self.proactive_timers.clear()
        proactive_message_buffers.clear()
        logger.info("ReplyDirectlyPlugin 已停用，所有计时器已取消。")
