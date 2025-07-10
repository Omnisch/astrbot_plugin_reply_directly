import asyncio
import json
import time
from typing import Dict, List, Set, Tuple

# 从 astrbot.api 导入所有必要的模块
from astrbot.api import logger, AstrBotConfig, MessageType
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# 使用您提供的仓库地址进行注册
@register("reply_directly", "qa296", "实现沉浸式对话和主动插话的插件", "1.0.0", "https://github.com/qa296/astrbot_plugin_reply_directly")
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 用于“沉浸式对话”功能，存储需要主动回复的会话ID
        self.sticky_reply_targets: Set[str] = set()
        
        # 用于“主动插话”功能
        # 存储每个群聊的最近消息记录 {group_id: [(timestamp, "sender: message")]}
        self.group_history: Dict[str, List[Tuple[int, str]]] = {}
        # 记录机器人最后一次在群里发言的时间 {group_id: timestamp}
        self.bot_last_spoke_time: Dict[str, float] = {}
        # 避免并发任务
        self.proactive_check_locks: Dict[str, asyncio.Lock] = {}

        logger.info("ReplyDirectlyPlugin 已加载。")

    # --- 功能1: 沉浸式对话 (Sticky Reply) ---

    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(self, event: AstrMessageEvent):
        """
        让机器人下次主动回复一次，无需@。仅在需要开启沉浸式对话，引导用户继续对话时使用。
        调用此函数后，机器人会记住当前会话，并在下一次该用户/群组发言时主动响应。
        """
        if not self.config.get("enable_plugin", True):
            return
        
        sticky_config = self.config.get("sticky_reply", {})
        if not sticky_config.get("enable", True):
            return

        session_id = event.unified_msg_origin
        if session_id:
            self.sticky_reply_targets.add(session_id)
            logger.info(f"[沉浸式对话] 已为会话 {session_id} 启用一次性主动回复。")
        
        # 根据需求，此函数不产生任何可见的回复
        # 所以我们不使用 yield event.plain_result()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def sticky_reply_handler(self, event: AstrMessageEvent):
        """
        监听所有消息，检查是否需要执行一次性主动回复。
        高优先级(priority=100)确保它在其他常规处理器之前运行。
        """
        if not self.config.get("enable_plugin", True):
            return
        
        sticky_config = self.config.get("sticky_reply", {})
        if not sticky_config.get("enable", True):
            return
        
        session_id = event.unified_msg_origin
        # 如果当前会话在我们的目标列表中
        if session_id in self.sticky_reply_targets:
            # 立即从目标中移除，确保只回复一次
            self.sticky_reply_targets.remove(session_id)
            logger.info(f"[沉浸式对话] 触发对 {session_id} 的主动回复，消息: '{event.message_str}'")
            
            # 停止事件继续传播，防止其他插件处理或默认的LLM调用（如果它不@机器人）
            event.stop_event()
            
            # 将此消息请求LLM进行处理，并将结果返回给用户
            yield event.request_llm(prompt=event.message_str)

    # --- 功能2: 主动插话 (Proactive Reply) ---
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=999)
    async def record_group_messages(self, event: AstrMessageEvent):
        """
        低优先级监听所有群消息，用于记录聊天历史。
        """
        # 无论功能是否开启，都记录历史，以便随时开启
        group_id = event.get_group_id()
        if not group_id:
            return

        if group_id not in self.group_history:
            self.group_history[group_id] = []
        
        history_limit = self.config.get("proactive_reply", {}).get("history_limit", 10)
        
        # 记录消息和时间戳
        record = (
            event.message_obj.timestamp,
            f"{event.get_sender_name()}: {event.message_str}"
        )
        self.group_history[group_id].append(record)
        
        # 保持历史记录在限制范围内
        self.group_history[group_id] = self.group_history[group_id][-history_limit:]

    @filter.after_message_sent()
    async def proactive_reply_trigger(self, event: AstrMessageEvent):
        """
        当机器人发送消息后触发，启动一个延时任务来检查后续聊天。
        """
        if not self.config.get("enable_plugin", True):
            return
            
        proactive_config = self.config.get("proactive_reply", {})
        if not proactive_config.get("enable", True):
            return
        
        # 此功能仅在群聊中生效
        if event.message_obj.type != MessageType.GROUP_MESSAGE:
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        logger.info(f"[主动插话] 机器人已在群 {group_id} 发言，准备启动延时检查任务。")
        self.bot_last_spoke_time[group_id] = time.time()
        
        # 创建一个异步任务来执行后续检查
        asyncio.create_task(self.check_chat_history(event))

    async def check_chat_history(self, original_event: AstrMessageEvent):
        """
        延时检查机器人在群里说话后的聊天记录，并决定是否插话。
        """
        proactive_config = self.config.get("proactive_reply", {})
        delay = proactive_config.get("delay_seconds", 5)
        
        group_id = original_event.get_group_id()
        if not group_id:
            return
            
        # 获取或创建锁
        if group_id not in self.proactive_check_locks:
            self.proactive_check_locks[group_id] = asyncio.Lock()
        
        async with self.proactive_check_locks[group_id]:
            # 等待设定的延迟时间
            await asyncio.sleep(delay)
            
            trigger_time = self.bot_last_spoke_time.pop(group_id, None)
            if not trigger_time:
                # 如果时间戳已被其他任务处理，则直接返回
                return

            history = self.group_history.get(group_id, [])
            
            # 筛选出机器人发言后的新消息
            recent_messages = [
                msg for ts, msg in history if ts > trigger_time
            ]
            
            if not recent_messages:
                logger.info(f"[主动插话] 在群 {group_id} 的 {delay}s 内无新消息，任务结束。")
                return

            logger.info(f"[主动插话] 在群 {group_id} 收集到 {len(recent_messages)} 条新消息，准备请求LLM判断。")
            
            # 准备LLM请求
            formatted_history = "\n".join(recent_messages)
            system_prompt = (
                "你是一个群聊观察助手。根据以下最近的聊天记录，"
                "请判断机器人是否需要主动插话进行回应、补充或引导话题。"
                "你的回答必须是一个JSON对象，格式如下: "
                '{"should_reply": boolean, "content": "string"}. '
                '如果需要插话，"should_reply"为true，"content"为要说的内容。'
                '如果不需要，"should_reply"为false。'
            )
            
            try:
                # 使用底层API调用LLM，因为它不会触发其他副作用
                llm_provider = self.context.get_using_provider()
                if not llm_provider:
                    logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                    return
                
                llm_response = await llm_provider.text_chat(
                    prompt=f"聊天记录:\n---\n{formatted_history}\n---",
                    system_prompt=system_prompt,
                    session_id=None, # 不关联任何特定对话
                )

                if llm_response and llm_response.completion_text:
                    logger.debug(f"[主动插話] LLM原始返回: {llm_response.completion_text}")
                    # 解析JSON
                    try:
                        # 尝试从文本中提取JSON块
                        json_str = llm_response.completion_text.strip()
                        if "```json" in json_str:
                           json_str = json_str.split("```json")[1].split("```")[0]
                        
                        data = json.loads(json_str)
                        if data.get("should_reply") and data.get("content"):
                            logger.info(f"[主动插话] LLM决定插话，内容: {data['content']}")
                            # 发送主动消息
                            chain = [Comp.Plain(data['content'])]
                            await self.context.send_message(original_event.unified_msg_origin, chain)
                        else:
                            logger.info("[主动插话] LLM决定不插话。")
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.error(f"[主动插话] 解析LLM返回的JSON失败: {e}\n原始文本: {llm_response.completion_text}")

            except Exception as e:
                logger.error(f"[主动插话] 请求LLM时发生未知错误: {e}")

    async def terminate(self):
        """插件停用或卸载时调用"""
        self.sticky_reply_targets.clear()
        self.group_history.clear()
        self.bot_last_spoke_time.clear()
        logger.info("ReplyDirectlyPlugin 已卸载，资源已清理。")
