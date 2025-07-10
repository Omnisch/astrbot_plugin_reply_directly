import asyncio
import time
import json
from collections import deque

# 导入AstrBot API，遵循开发文档规范
from astrbot.api.star import Star, Context, register
from astrbot.api.config import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api import logger

# 存储每个群组的聊天记录和主动回复任务
# key: group_id, value: collections.deque
group_chat_history = {}
# key: group_id, value: asyncio.Task
proactive_reply_tasks = {}
# 存储需要直接回复的群组
# key: group_id, value: True
direct_reply_targets = {}

@register(
    "reply_directly",
    "qa296",
    "一个通过函数调用和延时分析，让机器人实现沉浸式对话和主动插话的插件",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class ReplyDirectlyPlugin(Star):
    """
    实现沉浸式对话和主动插话功能，让群聊体验更自然。
    """
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info("ReplyDirectlyPlugin 已加载。")
        if not self.config.get("enable_plugin", False):
            logger.warning("ReplyDirectlyPlugin 已加载，但总开关处于关闭状态。")

    # 1. 沉浸式对话功能: LLM函数工具
    @filter.llm_tool()
    async def enable_direct_reply_once(self, event: AstrMessageEvent):
        """当LLM认为当前对话很热烈，或者需要引导下一轮对话时，调用此函数。机器人将在下一次群聊消息时主动回复，无需被@。此效果仅生效一次。"""
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return

        group_id = event.get_group_id()
        if not group_id:
            logger.debug("非群聊消息，无法启用直接回复。")
            return

        # 标记该群组的下一条消息需要直接回复
        direct_reply_targets[group_id] = True
        logger.info(f"[沉浸式对话] 已为群组 {group_id} 标记下一次直接回复。")

        # 根据要求，调用函数后不发送任何消息
        # 所以这里直接返回，不 yield 任何内容
        return

    # 2. 监听所有群消息，用于实现 "沉浸式对话" 和记录 "主动插话" 的历史
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin"):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        # ---------------- Part A: 实现沉浸式对话的回复 ----------------
        # 检查此群组是否被标记为需要直接回复
        if self.config.get("sticky_reply", {}).get("enable") and direct_reply_targets.get(group_id):
            logger.info(f"[沉浸式对话] 触发对群组 {group_id} 的直接回复。")
            # 使用后立即移除标记，确保只生效一次
            direct_reply_targets.pop(group_id, None)
            
            # 使用 event.request_llm 将当前消息交给LLM处理，实现回复
            yield event.request_llm(prompt=event.message_str, image_urls=event.get_image_urls())
            # 停止事件继续传播，避免后续的主动插话逻辑或其他插件处理
            event.stop_event()
            return
            
        # ---------------- Part B: 为主动插话功能记录历史 ----------------
        if self.config.get("proactive_reply", {}).get("enable"):
            # 初始化该群组的聊天记录队列
            if group_id not in group_chat_history:
                # 使用 deque 自动管理队列长度
                limit = self.config.get("proactive_reply", {}).get("history_limit", 10)
                group_chat_history[group_id] = deque(maxlen=limit)
            
            # 记录消息
            message_record = {
                "timestamp": time.time(),
                "sender": event.get_sender_name(),
                "text": event.message_str
            }
            group_chat_history[group_id].append(message_record)
            logger.debug(f"[主动插话-记录] 群 {group_id} 消息已记录: {message_record['sender']}: {message_record['text']}")


    # 3. 监听机器人自己发送的消息，用于触发 "主动插话" 的计时器
    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        """当机器人自己发送消息后，启动一个计时器来分析后续聊天。"""
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return
        
        # 只在群聊中生效
        if event.is_private_chat():
            return
            
        group_id = event.get_group_id()
        if not group_id:
            return

        # 如果该群组已经有一个正在计时的任务，先取消它
        if group_id in proactive_reply_tasks and not proactive_reply_tasks[group_id].done():
            proactive_reply_tasks[group_id].cancel()
            logger.debug(f"[主动插话-任务] 取消了群组 {group_id} 的旧任务。")

        # 创建一个新的异步任务，在指定延迟后执行
        delay = self.config.get("proactive_reply", {}).get("delay_seconds", 5)
        task = asyncio.create_task(self._proactive_reply_task(group_id, delay, event.unified_msg_origin))
        proactive_reply_tasks[group_id] = task
        logger.info(f"[主动插话-任务] 已为群组 {group_id} 创建新的主动回复任务，将在 {delay} 秒后执行。")


    async def _proactive_reply_task(self, group_id: str, delay: int, unified_msg_origin: str):
        """
        延时任务：获取指定时间内的聊天记录，请求LLM判断是否回复，并执行回复。
        """
        try:
            # 记录任务开始的时间戳
            task_start_time = time.time()
            await asyncio.sleep(delay)

            # 获取这段时间内的聊天记录
            if group_id not in group_chat_history:
                return
            
            # 筛选出机器人说话之后、任务执行之前的新消息
            recent_messages = [
                msg for msg in group_chat_history[group_id] 
                if msg["timestamp"] > (task_start_time - delay)
            ]

            if not recent_messages:
                logger.debug(f"[主动插话-分析] 群组 {group_id} 在 {delay}s 内无新消息，任务结束。")
                return
            
            # 格式化历史记录以供LLM分析
            history_str = "\n".join([f"{msg['sender']}: {msg['text']}" for msg in recent_messages])
            logger.info(f"[主动插话-分析] 群组 {group_id} 的近期聊天记录:\n{history_str}")

            # 构建请求LLM的Prompt
            system_prompt = (
                "你是一个群聊观察员，你的任务是分析一段在机器人发言后的聊天记录，并判断机器人是否应该主动插话，以促进对话。 "
                "请根据以下聊天内容，决定是否需要回复以及回复什么。 "
                "你的回答必须是一个严格的JSON格式，包含两个字段：\n"
                "1. `should_reply` (boolean): 如果你认为应该插话，则为 true，否则为 false。\n"
                "2. `content` (string): 如果 `should_reply` 为 true，这里是你要回复的内容；否则为空字符串。\n"
                "例如: {\"should_reply\": true, \"content\": \"听起来很有趣！我也想加入讨论。\"} 或 {\"should_reply\": false, \"content\": \"\"}\n"
                "不要在JSON之外添加任何额外的解释或文字。"
            )
            
            prompt = f"这是机器人发言后 {delay} 秒内的聊天记录：\n\n---\n{history_str}\n---"

            # 使用底层API调用LLM，避免触发其他钩子
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话-LLM] 未找到正在使用的大语言模型提供商。")
                return

            llm_response = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                contexts=[] # 不依赖之前的上下文，只分析当前片段
            )

            # 解析LLM的回复
            response_text = llm_response.completion_text.strip()
            logger.debug(f"[主动插话-LLM] 收到LLM原始响应: {response_text}")

            try:
                # 尝试从可能的代码块中提取json
                if "```json" in response_text:
                    response_text = response_text.split("```json")[1].split("```")[0].strip()

                decision = json.loads(response_text)
                should_reply = decision.get("should_reply", False)
                content_to_send = decision.get("content", "")

                if should_reply and content_to_send:
                    logger.info(f"[主动插话-执行] LLM决定插话，内容: {content_to_send}")
                    # 使用 context.send_message 主动发送消息
                    await self.context.send_message(unified_msg_origin, [Plain(text=content_to_send)])
                else:
                    logger.info("[主动插话-执行] LLM决定不插话。")

            except (json.JSONDecodeError, AttributeError, KeyError) as e:
                logger.error(f"[主动插话-解析] 解析LLM响应失败: {e}\n原始响应: {response_text}")

        except asyncio.CancelledError:
            logger.debug(f"[主动插话-任务] 群组 {group_id} 的任务被取消。")
        except Exception as e:
            logger.error(f"[主动插话-任务] 执行主动回复任务时发生未知错误: {e}")
        finally:
            # 任务结束后，从字典中移除
            proactive_reply_tasks.pop(group_id, None)
