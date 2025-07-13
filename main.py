import asyncio
import json
import re
from collections import defaultdict
from asyncio import Lock

from astrbot.api.event import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig, MessageEventResult
import astrbot.api.message_components as Comp

@register(
    "reply_directly",
    "qa296",
    "让您的 AstrBot 在群聊中变得更加生动和智能！本插件使其可以主动的连续交互。",
    "1.1.0", 
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        # 默认配置
        self.default_config = {
            'enable_plugin': True,
            'enable_immersive_chat': True,
            'enable_proactive_reply': True,
            'proactive_reply_delay': 8
        }
        # 合并用户配置和默认配置
        self.config = {**self.default_config, **config}

        # 沉浸式对话：存储需要直接回复的群组及其上下文
        self.direct_reply_context = {} 
        # 主动插话：active_timers存储后台的循环任务
        self.active_timers = {}
        # 主动插话：group_chat_buffer存储群聊消息
        self.group_chat_buffer = defaultdict(list)

        # [新增] 引入异步锁，防止并发操作导致的数据冲突
        self.immersive_lock = Lock()
        self.proactive_lock = Lock()
        
        logger.info("ReplyDirectly插件 v1.1.0 加载成功！")

    def _extract_json_from_text(self, text: str) -> str:
        """从文本中提取JSON字符串，兼容代码块和裸露的JSON。"""
        pattern = r'```json\s*(.*?)\s*```'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return text[start:end+1].strip()
        return text.strip()

    # --- 功能1: 沉浸式对话 (Immersive Chat) ---

    @filter.llm_tool()
    async def enable_direct_reply_once(self, event: AstrMessageEvent):
        """
        【LLM工具】当LLM认为可以开启沉浸式对话时调用此函数。
        这会让机器人在该群组的下一条消息时直接回复，无需@。此效果仅生效一次。
        """
        if not self.config.get('enable_immersive_chat', True):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        try:
            uid = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                logger.warning(f"[沉浸式对话] 无法获取群 {group_id} 的当前会话ID，无法保存上下文。")
                return
            
            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            context_history = json.loads(conversation.history) if conversation and conversation.history else []
            
            # 使用锁保证数据安全
            async with self.immersive_lock:
                self.direct_reply_context[group_id] = {
                    "cid": curr_cid,
                    "context": context_history
                }
            logger.info(f"[沉浸式对话] 已为群 {group_id} 开启单次直接回复模式，并保存了当前对话上下文。")
        except Exception as e:
            logger.error(f"[沉浸式对话] 保存上下文时出错: {e}", exc_info=True)

    # --- 功能2: 主动插话 (Proactive Interjection) ---

    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        """机器人发送消息后，启动或重置主动插话的计时器。"""
        if not self.config.get('enable_plugin', True) or not self.config.get('enable_proactive_reply', True):
            return
        if event.is_private_chat():
            return
        
        group_id = event.get_group_id()
        if not group_id:
            return

        # 使用锁保护对active_timers和group_chat_buffer的访问
        async with self.proactive_lock:
            # 如果存在旧的计时任务，先取消它
            if group_id in self.active_timers:
                self.active_timers[group_id].cancel()
                logger.debug(f"[主动插话] 取消了群 {group_id} 的旧计时器。")

            # 清空该群的消息缓冲区并启动新的计时任务
            self.group_chat_buffer[group_id].clear()
            task = asyncio.create_task(self._proactive_check_task(group_id, event.unified_msg_origin))
            self.active_timers[group_id] = task
        
        logger.info(f"[主动插话] 机器人发言，已为群 {group_id} 重置主动插话计时器。")

    async def _proactive_check_task(self, group_id: str, unified_msg_origin: str):
        """后台计时任务，用于判断是否需要主动插话。"""
        inactivity_counter = 0
        # [新增] 如果连续3个周期都没有任何消息，任务将自动退出以节省资源
        max_inactivity_cycles = 3 
        try:
            while True:
                delay = self.config.get('proactive_reply_delay', 8)
                await asyncio.sleep(delay)

                async with self.proactive_lock:
                    chat_history = self.group_chat_buffer.pop(group_id, [])
                
                if not chat_history:
                    inactivity_counter += 1
                    logger.debug(f"[主动插话] 群 {group_id} 在 {delay}s 内无新消息。空闲周期: {inactivity_counter}/{max_inactivity_cycles}")
                    if inactivity_counter >= max_inactivity_cycles:
                        logger.info(f"[主动插话] 群 {group_id} 持续空闲，自动停止监听任务。")
                        break # 退出循环，任务将结束
                    continue
                
                inactivity_counter = 0 # 有消息，重置空闲计数器
                logger.info(f"[主动插话] 群 {group_id} 计时结束，收集到 {len(chat_history)} 条消息，请求LLM判断。")
                
                formatted_history = "\n".join(chat_history)
                prompt = (
                    f"我在一个群聊里，在我说完话后，群里发生了以下的对话：\n"
                    f"--- 对话记录 ---\n{formatted_history}\n--- 对话记录结束 ---\n"
                    f"请判断我是否应该插话。请严格按照JSON格式在```json ... ```代码块中回答，不要有任何其他说明文字。\n"
                    f'格式示例：\n```json\n{{"should_reply": true, "content": "你的回复内容"}}\n```\n'
                    f'或\n```json\n{{"should_reply": false, "content": ""}}\n```'
                )

                provider = self.context.get_using_provider()
                if not provider:
                    logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                    continue

                llm_response = await provider.text_chat(prompt=prompt)
                json_string = self._extract_json_from_text(llm_response.completion_text)
                if not json_string:
                    logger.warning(f"[主动插话] 从LLM回复中未能提取出JSON。原始回复: {llm_response.completion_text}")
                    continue

                try:
                    decision_data = json.loads(json_string)
                    if decision_data.get("should_reply") and decision_data.get("content"):
                        content = decision_data["content"]
                        logger.info(f"[主动插话] LLM判断需要回复，内容: {content[:50]}...")
                        message_chain = MessageChain().message(content)
                        await self.context.send_message(unified_msg_origin, message_chain) 
                    else:
                        logger.info("[主动插话] LLM判断无需回复。")
                except (json.JSONDecodeError, TypeError, AttributeError) as e:
                    logger.error(f"[主动插话] 解析LLM的JSON回复失败: {e}\n原始回复: {llm_response.completion_text}\n清理后文本: '{json_string}'")
        
        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的后台任务被取消。")
        except Exception as e:
            logger.error(f"[主动插话] 群 {group_id} 的后台任务出现未知异常: {e}", exc_info=True)
        finally:
            # 任务结束时，从active_timers中移除自己
            async with self.proactive_lock:
                self.active_timers.pop(group_id, None)
                self.group_chat_buffer.pop(group_id, None)

    # --- 统一的消息监听器 ---

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> MessageEventResult:
        """统一处理所有群聊消息。"""
        if not self.config.get('enable_plugin', True):
            return

        group_id = event.get_group_id()
        # 忽略机器人自己的消息
        if not group_id or event.get_sender_id() == event.get_self_id():
            return

        # --- 逻辑1: 检查是否处于沉浸式对话模式 ---
        if self.config.get('enable_immersive_chat', True):
            async with self.immersive_lock:
                if group_id in self.direct_reply_context:
                    logger.info(f"[沉浸式对话] 检测到群 {group_id} 的直接回复消息，将携带上下文触发LLM。")
                    saved_data = self.direct_reply_context.pop(group_id)
            
                    event.stop_event() # 阻止事件继续传播，避免默认的LLM回复
                    # 使用 event.request_llm 并传入上下文和会话ID，触发带上下文的回复
                    yield event.request_llm(
                        prompt=event.message_str,
                        contexts=saved_data.get("context", []),
                        session_id=saved_data.get("cid")
                    )
                    return # 处理完毕，直接返回

        # --- 逻辑2: 为主动插话功能记录消息 ---
        if self.config.get('enable_proactive_reply', True):
            async with self.proactive_lock:
                # 如果是新活跃的群或任务已休眠，启动一个新任务
                if group_id not in self.active_timers:
                    logger.info(f"[主动插话] 检测到群 {group_id} 新消息，启动后台监听任务。")
                    task = asyncio.create_task(self._proactive_check_task(group_id, event.unified_msg_origin))
                    self.active_timers[group_id] = task

                # 记录消息到缓冲区
                sender_name = event.get_sender_name() or event.get_sender_id()
                message_text = event.message_str.strip()
                if message_text:
                    # 限制缓冲区大小，防止内存溢出
                    if len(self.group_chat_buffer[group_id]) < 20: 
                        self.group_chat_buffer[group_id].append(f"{sender_name}: {message_text}")

    async def terminate(self):
        """插件被卸载/停用时调用，用于清理所有后台任务。"""
        logger.info("正在卸载ReplyDirectly插件，取消所有后台任务...")
        # 使用锁来安全地迭代和修改
        async with self.proactive_lock:
            for task in self.active_timers.values():
                task.cancel()
            self.active_timers.clear()
            self.group_chat_buffer.clear()
        
        async with self.immersive_lock:
            self.direct_reply_context.clear()
            
        logger.info("ReplyDirectly插件所有后台任务和数据已清理。")
