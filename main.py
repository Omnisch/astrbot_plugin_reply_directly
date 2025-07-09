import asyncio
import json
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# --- DEBUGGING CONSTANTS ---
DEBUG_PREFIX = "[DEBUG-ReplyDirectly]"

@register(
    "ReplyDirectly",
    "YourName (Debug Version)",
    "一个实现主动回复和追问/补充回复的插件 (带详细日志)",
    "1.0.1",
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.proactive_reply_targets = set()
        logger.info("主动&补充回复插件已加载 (Debug模式)。")
        logger.debug(f"{DEBUG_PREFIX} 插件初始化，当前配置: {json.dumps(self.config, indent=2)}")

    # 功能1: 通过LLM函数工具标记用户
    @filter.llm_tool(name="mark_user_for_proactive_reply")
    async def mark_user_for_proactive_reply(self, event: AstrMessageEvent, reason: str = "No reason provided") -> MessageEventResult:
        """当LLM认为和用户聊得火热，希望在用户下次发言时主动回应时，调用此工具。这会标记用户，使得机器人下次能主动回复一次。

        Args:
            reason (string): 简要说明为什么要标记用户，例如“用户情绪高涨，适合继续话题”。
        """
        logger.debug(f"{DEBUG_PREFIX} LLM工具 'mark_user_for_proactive_reply' 被调用。原因: '{reason}'")
        
        proactive_config = self.config.get("proactive_reply", {})
        if not proactive_config.get("enable", False):
            logger.warning(f"{DEBUG_PREFIX} 主动回复功能已禁用，但LLM尝试调用工具。")
            return event.plain_result("The proactive reply feature is currently disabled.")

        umo = event.unified_msg_origin
        logger.debug(f"{DEBUG_PREFIX} 标记前的目标列表: {self.proactive_reply_targets}")
        self.proactive_reply_targets.add(umo)
        logger.info(f"{DEBUG_PREFIX} [主动回复] 已标记用户 {umo} 进行下一次主动回复。")
        logger.debug(f"{DEBUG_PREFIX} 标记后的目标列表: {self.proactive_reply_targets}")
        
        return event.plain_result("OK, I have marked the user for a proactive reply on their next message.")

    # 功能1: 监听所有消息，实现主动回复
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def proactive_reply_handler(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        logger.debug(f"{DEBUG_PREFIX} [主动回复处理器] 收到消息 from {umo}. Is wake: {event.is_at_or_wake_command}. Message: '{event.message_str[:50]}...'")

        proactive_config = self.config.get("proactive_reply", {})
        if not proactive_config.get("enable", False):
            logger.debug(f"{DEBUG_PREFIX} 主动回复功能禁用，跳过处理。")
            return
            
        logger.debug(f"{DEBUG_PREFIX} 当前待回复目标: {self.proactive_reply_targets}")

        is_target = umo in self.proactive_reply_targets
        is_not_wake_command = not event.is_at_or_wake_command

        logger.debug(f"{DEBUG_PREFIX} 条件检查: Is Target? {is_target}, Is Not Wake Command? {is_not_wake_command}")
        
        if is_target and is_not_wake_command:
            logger.info(f"{DEBUG_PREFIX} [主动回复] 检测到被标记用户 {umo} 发言，将主动回复。")
            
            self.proactive_reply_targets.remove(umo)
            logger.debug(f"{DEBUG_PREFIX} 已从目标列表移除 {umo}. 剩余目标: {self.proactive_reply_targets}")
            
            logger.debug(f"{DEBUG_PREFIX} 正在停止事件传播，并请求LLM进行回复。")
            event.stop_event()
            yield event.request_llm(prompt=event.message_str)
        else:
            logger.debug(f"{DEBUG_PREFIX} 不满足主动回复条件，传递事件。")


    # 功能2: 监听机器人发送消息后的事件
    @filter.after_message_sent()
    async def after_message_sent_handler(self, event: AstrMessageEvent):
        logger.debug(f"{DEBUG_PREFIX} [补充回复钩子] 'after_message_sent' 触发。")
        
        follow_up_config = self.config.get("follow_up_reply", {})
        if not follow_up_config.get("enable", False):
            logger.debug(f"{DEBUG_PREFIX} 补充回复功能禁用，跳过。")
            return

        result = event.get_result()
        if result and result.is_send:
            logger.debug(f"{DEBUG_PREFIX} Bot已发送消息，准备启动补充回复的后台任务。")
            asyncio.create_task(self._handle_follow_up(event))
        else:
            logger.debug(f"{DEBUG_PREFIX} 钩子触发，但Bot未发送消息 (result.is_send is False)，跳过。")


    async def _handle_follow_up(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        logger.debug(f"{DEBUG_PREFIX} [补充回复任务] 后台任务启动 for UMO: {umo}")
        try:
            conf = self.config.get("follow_up_reply", {})
            delay = conf.get("delay_seconds", 5)
            
            logger.debug(f"{DEBUG_PREFIX} 等待 {delay} 秒...")
            await asyncio.sleep(delay)
            logger.debug(f"{DEBUG_PREFIX} 等待结束，开始处理。")

            bot_message_result = event.get_result()
            bot_message_str = "".join([c.text for c in bot_message_result.chain if isinstance(c, Comp.Plain)])
            if not bot_message_str:
                logger.debug(f"{DEBUG_PREFIX} Bot发送的消息不含文本，任务终止。")
                return

            user_message_str = event.message_str
            logger.debug(f"{DEBUG_PREFIX} 用户原消息: '{user_message_str}'")
            logger.debug(f"{DEBUG_PREFIX} Bot的回复: '{bot_message_str}'")

            history_str = "[]"
            try:
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
                    if conversation and conversation.history:
                        history_str = conversation.history # This is already a JSON string
                        logger.debug(f"{DEBUG_PREFIX} 成功获取到对话历史。长度: {len(json.loads(history_str))}")
                else:
                    logger.debug(f"{DEBUG_PREFIX} 未找到当前对话ID，无法获取历史记录。")
            except Exception as e:
                logger.warning(f"{DEBUG_PREFIX} 获取对话历史时出错: {e}")

            prompt_template = conf.get("prompt", "")
            final_prompt = prompt_template.format(
                user_message=user_message_str,
                bot_message=bot_message_str,
                history=json.dumps(json.loads(history_str), ensure_ascii=False, indent=2)
            )
            logger.debug(f"{DEBUG_PREFIX} --- 发送给LLM的完整Prompt ---\n{final_prompt}\n--------------------------------")

            provider = self.context.get_using_provider()
            if not provider:
                logger.error(f"{DEBUG_PREFIX} 未找到正在使用的LLM Provider，无法执行补充回复。")
                return

            llm_response = await provider.text_chat(prompt=final_prompt)
            response_text = llm_response.completion_text
            logger.debug(f"{DEBUG_PREFIX} --- LLM的原始响应 ---\n{response_text}\n---------------------------")

            # Clean up potential markdown code block
            if "```json" in response_text:
                try:
                    response_text = response_text.split("```json")[1].split("```")[0].strip()
                    logger.debug(f"{DEBUG_PREFIX} 已从Markdown代码块中提取JSON。")
                except IndexError:
                    logger.warning(f"{DEBUG_PREFIX} 尝试提取JSON失败，将直接解析原文。")
            
            try:
                data = json.loads(response_text)
                logger.debug(f"{DEBUG_PREFIX} JSON解析成功: {data}")
                if data.get("should_reply") and data.get("content"):
                    content = data['content']
                    logger.info(f"{DEBUG_PREFIX} [补充回复] LLM决定进行补充: '{content}'")
                    message_chain = [Comp.Plain(text=content)]
                    await self.context.send_message(umo, message_chain)
                else:
                    logger.debug(f"{DEBUG_PREFIX} LLM决定不进行补充。should_reply: {data.get('should_reply')}, content: '{data.get('content')}'")
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.error(f"{DEBUG_PREFIX} 解析LLM的JSON响应失败: {e}。确认LLM是否返回了严格的JSON格式。失败的原文: '{response_text}'")

        except Exception as e:
            logger.error(f"{DEBUG_PREFIX} 处理补充回复时发生未知错误: {e}", exc_info=True)
            
    async def terminate(self):
        logger.info("主动&补充回复插件 (Debug模式) 正在卸载...")
        logger.debug(f"{DEBUG_PREFIX} 清理标记用户列表: {self.proactive_reply_targets}")
        self.proactive_reply_targets.clear()
        logger.info("插件清理完毕。")
