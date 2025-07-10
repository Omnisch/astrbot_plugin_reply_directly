import asyncio
import json
from typing import Dict, List

from astrbot.api import logger, AstrBotConfig, MessageType
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# --- 插件元数据 ---
PLUGIN_NAME = "reply_directly"
PLUGIN_AUTHOR = "qa296"
PLUGIN_DESC = "实现沉浸式对话和主动插话功能"
PLUGIN_VERSION = "1.0.0"
PLUGIN_REPO = "https://github.com/qa296/astrbot_plugin_reply_directly"

# --- 主动插话功能的 LLM Prompt ---
PROACTIVE_REPLY_PROMPT_TEMPLATE = """
你是一个敏锐的群聊观察者。你的任务是分析一小段聊天记录，并判断自己是否应该主动插话。

规则:
1.  只有当聊天内容与你（机器人）之前讨论的话题高度相关，或者是一个你（机器人）能提供巨大价值的新话题时，才应该插话。
2.  避免在无关紧要的闲聊、打招呼、表情包斗图中插话。
3.  你的回答必须是严格的 JSON 格式，不包含任何其他解释性文本。

聊天记录如下:
---
{chat_log}
---

请根据以上内容，以严格的 JSON 格式返回你的判断和回复内容。
JSON 格式:
{{
  "should_reply": <true 或 false>,
  "reply_content": "<如果 should_reply 为 true，这里是你的回复内容，否则为空字符串>"
}}
"""


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 用于沉浸式对话的状态存储
        # key: f"{platform_name}:{user_id}", value: True
        self.sticky_reply_users: Dict[str, bool] = {}

        # 用于主动插话的状态存储
        # key: group_id, value: list of messages
        self.proactive_listeners: Dict[str, List[str]] = {}
        # key: group_id, value: asyncio.Task
        self.proactive_tasks: Dict[str, asyncio.Task] = {}
        
        logger.info("ReplyDirectlyPlugin 已加载。")


    # ----------------------------------------------------------------
    # 功能 1: 沉浸式对话 (Sticky Reply)
    # ----------------------------------------------------------------

    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(self, event: AstrMessageEvent) -> None:
        """
        当与用户对话渐入佳境时，调用此函数可以让机器人在下一次无需@就能主动回复该用户一次。
        函数调用后不会发送任何特定消息。
        """
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return

        user_id = event.get_sender_id()
        platform = event.get_platform_name()
        user_key = f"{platform}:{user_id}"

        self.sticky_reply_users[user_key] = True
        logger.info(f"[沉浸式对话] 已为用户 {user_key} 开启一次性主动回复。")
        # 根据需求，这里不 yield 任何 MessageEventResult，以避免发送额外消息

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_sticky_reply(self, event: AstrMessageEvent):
        """
        捕获所有消息，检查是否是沉浸式对话的目标。
        高优先级确保在默认的LLM处理之前执行。
        """
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return
        
        # 如果消息是@机器人或者来自私聊，则正常流程处理，不应消耗沉浸式对话次数
        if event.is_at_or_wake_command:
            return

        user_id = event.get_sender_id()
        platform = event.get_platform_name()
        user_key = f"{platform}:{user_id}"

        if self.sticky_reply_users.get(user_key):
            logger.info(f"[沉浸式对话] 捕获到用户 {user_key} 的消息，将主动回复。")
            # 标记已使用，确保只生效一次
            del self.sticky_reply_users[user_key]

            # 触发LLM进行回复
            yield event.request_llm(prompt=event.message_str)
            # 停止事件传播，防止后续处理器（如默认LLM调用）再次响应
            event.stop_event()


    # ----------------------------------------------------------------
    # 功能 2: 主动插话 (Proactive Reply)
    # ----------------------------------------------------------------

    @filter.after_message_sent()
    async def on_bot_message_sent(self, event: AstrMessageEvent):
        """
        当机器人发送消息后触发，用于启动主动插话的监听器。
        """
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return

        # 此功能只在群聊中生效
        if event.message_obj.type != MessageType.GROUP_MESSAGE:
            return
            
        group_id = event.get_group_id()
        if not group_id:
            return

        # 如果该群聊已有监听任务，先取消旧的
        if group_id in self.proactive_tasks:
            self.proactive_tasks[group_id].cancel()

        logger.info(f"[主动插话] 机器人已在群 {group_id} 发言，开始监听后续消息。")
        # 初始化监听列表
        self.proactive_listeners[group_id] = []
        
        # 创建一个延迟检查任务
        task = asyncio.create_task(self._check_proactive_reply(event, group_id))
        self.proactive_tasks[group_id] = task


    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def collect_proactive_history(self, event: AstrMessageEvent):
        """
        收集群聊消息，为主动插话功能提供上下文。
        """
        # 即使主开关关闭，也需要让它运行以避免字典键错误，但在 on_bot_message_sent 中会阻止启动
        group_id = event.get_group_id()
        
        # 如果该群在监听列表中，并且不是机器人自己发的消息
        if group_id in self.proactive_listeners and event.get_sender_id() != event.get_self_id():
            sender_name = event.get_sender_name() or event.get_sender_id()
            message_text = event.message_str
            
            # 限制历史消息数量
            history_limit = self.config.get("proactive_reply", {}).get("history_limit", 10)
            if len(self.proactive_listeners[group_id]) < history_limit:
                self.proactive_listeners[group_id].append(f"{sender_name}: {message_text}")


    async def _check_proactive_reply(self, event: AstrMessageEvent, group_id: str):
        """
        延迟后执行的检查任务
        """
        try:
            delay_seconds = self.config.get("proactive_reply", {}).get("delay_seconds", 5)
            await asyncio.sleep(delay_seconds)

            # 从监听字典中弹出历史记录，确保任务只执行一次
            history = self.proactive_listeners.pop(group_id, None)
            self.proactive_tasks.pop(group_id, None)

            if not history:
                logger.info(f"[主动插话] 群 {group_id} 在 {delay_seconds}s 内无新消息，任务结束。")
                return

            chat_log = "\n".join(history)
            logger.info(f"[主动插话] 群 {group_id} 收集到聊天记录:\n{chat_log}")

            # 调用LLM进行判断
            prompt = PROACTIVE_REPLY_PROMPT_TEMPLATE.format(chat_log=chat_log)
            
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                return
            
            llm_response: LLMResponse = await provider.text_chat(prompt=prompt)
            
            response_text = llm_response.completion_text
            if not response_text:
                logger.info("[主动插话] LLM未返回任何内容。")
                return
                
            # 解析LLM的JSON输出
            try:
                # 尝试修复LLM可能返回的被```json ... ```包裹的代码块
                if response_text.strip().startswith("```json"):
                    response_text = response_text.strip()[7:-3].strip()
                
                decision = json.loads(response_text)
                should_reply = decision.get("should_reply", False)
                reply_content = decision.get("reply_content", "")

                if should_reply and reply_content:
                    logger.info(f"[主动插话] LLM决定在群 {group_id} 插话，内容: {reply_content}")
                    # 主动发送消息
                    message_chain = [Plain(text=reply_content)]
                    await self.context.send_message(event.unified_msg_origin, message_chain)
                else:
                    logger.info(f"[主动插话] LLM决定不在群 {group_id} 插话。")

            except json.JSONDecodeError:
                logger.error(f"[主动插话] 解析LLM返回的JSON失败: {response_text}")
            except Exception as e:
                logger.error(f"[主动插话] 处理LLM响应时发生未知错误: {e}")

        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的监听任务被取消。")
        except Exception as e:
            logger.error(f"[主动插话] 检查任务发生意外错误: {e}")


    async def terminate(self):
        """
        插件卸载或停用时调用，用于清理资源。
        """
        logger.info("ReplyDirectlyPlugin 正在卸载...")
        # 清理状态字典
        self.sticky_reply_users.clear()
        self.proactive_listeners.clear()
        # 取消所有正在运行的异步任务
        for task in self.proactive_tasks.values():
            task.cancel()
        self.proactive_tasks.clear()
        logger.info("ReplyDirectlyPlugin 已清理资源并卸载。")
