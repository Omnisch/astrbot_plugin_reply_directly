import asyncio
import json
import re

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

@register(
    "reply_directly",
    "qa296",
    "让您的 AstrBot 在群聊中变得更加生动和智能！本插件使其可以主动的连续交互。",
    "1.0.1", 
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        self.default_config = {
            'enable_plugin': True,
            'enable_immersive_chat': True,
            'enable_proactive_reply': True,
            'proactive_reply_interval': 8
        }
        self.config = {**self.default_config, **config}

        # [修改] 沉浸式对话：从 set 改为 dict，用于存储上下文
        self.direct_reply_context = {}
        # 主动插话：active_counters 存储后台的计数器
        self.active_counters = {}
        self.active_task = None
        logger.info("ReplyDirectly插件 v1.1.0 加载成功！")

    def _extract_json_from_text(self, text: str) -> str:
        pattern = r'```json\s*(.*?)\s*```'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return text[start:end+1].strip()
        return text.strip()

    # -----------------------------------------------------
    # Feature 1: 沉浸式对话 (Immersive Chat) - [已增强]
    # -----------------------------------------------------

    @filter.llm_tool()
    async def enable_direct_reply_once(self, event: AstrMessageEvent):
        """
        当 LLM 认为可以开启沉浸式对话时调用此函数。这会让机器人在该群组的下一条消息时直接回复，无需 @。此效果仅生效一次。
        """
        if not self.config.get('enable_immersive_chat', True):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        # [新增] 获取并存储当前对话的完整上下文
        try:
            uid = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                logger.warning(f"[沉浸式对话] 无法获取群 {group_id} 的当前会话 ID，无法保存上下文。")
                return
            
            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            context = json.loads(conversation.history) if conversation and conversation.history else []
            
            self.direct_reply_context[group_id] = {
                "cid": curr_cid,
                "context": context
            }
            logger.info(f"[沉浸式对话] 已为群 {group_id} 开启单次直接回复模式，并保存了当前对话上下文。")
        except Exception as e:
            logger.error(f"[沉浸式对话] 保存上下文时出错: {e}", exc_info=True)


    # -----------------------------------------------------
    # Feature 2: 主动插话 (Proactive Interjection) - [已重构]
    # -----------------------------------------------------

    async def _proactive_check_task(self, group_id: str, uid: str):
        """检测是否需要主动插话"""
        try:
            logger.info(f"[主动插话] 群 {group_id} 开始请求 LLM 判断。")

            func_tool_mgr = self.context.get_llm_tool_manager()

            # 获取用户当前与 LLM 的对话以获得上下文信息
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            conversation = None
            context = []
            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
                if conversation and conversation.history:
                    context = json.loads(conversation.history)

            prompt = (
                f"在生成回复前，根据上下文判断你是否应该说话。严格按照 JSON 格式在```json ... ```代码块中回答，禁止任何其他说明文字。\n"
                f'格式示例：\n```json\n{{"should_reply": true, "content": "<REPLY_CONTENT>"}}\n```\n'
                f'或\n```json\n{{"should_reply": false, "content": ""}}\n```'
            )

            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                return

            llm_response = await provider.text_chat(
                prompt=prompt,
                contexts=context,
                func_tool=func_tool_mgr,
                system_prompt=""
            )
            
            json_string = self._extract_json_from_text(llm_response.completion_text)
            if not json_string:
                logger.warning(f"[主动插话] 从 LLM 回复中未能提取出 JSON。原始回复: {llm_response.completion_text}")
                return

            try:
                decision_data = json.loads(json_string)
                should_reply = decision_data.get("should_reply", False)
                content = decision_data.get("content", "")

                if should_reply and content:
                    logger.info(f"[主动插话] LLM 判断需要回复，内容: {content[:50]}...")
                    message_chain = MessageChain().message(content)
                    await self.context.send_message(uid, message_chain)
                else:
                    logger.info("[主动插话] LLM 判断无需回复。")
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                logger.error(f"[主动插话] 解析 LLM 的 JSON 回复失败: {e}\n原始回复: {llm_response.completion_text}\n清理后文本: '{json_string}'")
        
        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的循环检测任务被取消。")
        except Exception as e:
            logger.error(f"[主动插话] 群 {group_id} 的循环检测任务出现未知异常: {e}", exc_info=True)

    # -----------------------------------------------------
    # 统一的消息监听器 - [已修改]
    # -----------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """统一处理所有群聊消息"""
        if not self.config.get('enable_plugin', True):
            return

        group_id = event.get_group_id()
        if not group_id or event.get_sender_id() == event.get_self_id():
            return

        # 逻辑1: [增强] 检查是否处于沉浸式对话模式
        if self.config.get('enable_immersive_chat', True) and group_id in self.direct_reply_context:
            logger.info(f"[沉浸式对话] 检测到群 {group_id} 的直接回复消息，将携带上下文触发 LLM。")
            
            # [修改] 弹出保存的上下文信息
            saved_data = self.direct_reply_context.pop(group_id)
            saved_cid = saved_data.get("cid")
            saved_context = saved_data.get("context", [])

            event.stop_event()
            # [修改] 使用 event.request_llm 并传入上下文和会话ID
            yield event.request_llm(
                prompt=event.message_str,
                contexts=saved_context,
                session_id=saved_cid
            )
            return

        # 逻辑2: [修改] 为主动插话功能提供支持
        if self.config.get('enable_proactive_reply', True):
            # 如果没有计数器，说明是新活跃的群，启动一个
            if group_id not in self.active_counters:
                logger.info(f"[主动插话] 检测到群 {group_id} 新消息，启动消息计数。")
                self.active_counters[group_id] = 0

            self.active_counters[group_id] += 1
            
            if self.active_counters[group_id] >= self.config.get('proactive_reply_interval', 8):
                self.active_task = asyncio.create_task(self._proactive_check_task(group_id, event.unified_msg_origin))
                logger.info(f"[主动插话] 群 {group_id} 的消息计数已达到 {self.active_counters[group_id]}。")
                self.active_counters[group_id] = 0


    async def terminate(self):
        """插件被卸载/停用时调用，用于清理"""
        logger.info("正在卸载 ReplyDirectly 插件，取消所有后台循环任务...")
        if self.active_task:
            self.active_task.cancel()
        self.active_counters.clear()
        self.direct_reply_context.clear()
        logger.info("ReplyDirectly 插件所有后台任务已清理。")
