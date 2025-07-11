import asyncio
import json
import re
from collections import defaultdict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp

@register(
    "reply_directly",
    "qa296",
    "提供沉浸式对话和主动插话功能，让机器人更智能地参与群聊。",
    "1.0.1",
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

        self.direct_reply_groups = set()
        self.active_timers = {}
        self.group_chat_buffer = defaultdict(list)
        logger.info("ReplyDirectly插件加载成功！")

    def _extract_json_from_text(self, text: str) -> str:
        """
        从可能包含Markdown代码块的文本中稳健地提取纯JSON字符串。
        """
        # 模式1: 优先匹配 ```json ... ``` 格式
        pattern1 = r'```json\s*(.*?)\s*```'
        match = re.search(pattern1, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 模式2: 其次匹配 ``` ... ``` 格式 (没有 'json' 标识)
        pattern2 = r'```\s*(.*?)\s*```'
        match = re.search(pattern2, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 模式3: 如果没有找到Markdown块，尝试寻找第一个 '{' 和最后一个 '}'
        # 这可以处理一些不规范但仍包含JSON的回复
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return text[start:end+1].strip()

        # 如果以上都不行，返回原始文本的strip版本，让json.loads自己去尝试最后一次
        return text.strip()

    # -----------------------------------------------------
    # Feature 1: 沉浸式对话 (Immersive Chat)
    # -----------------------------------------------------

    @filter.llm_tool()
    async def enable_direct_reply_once(self, event: AstrMessageEvent):
        """
        当LLM认为可以开启沉浸式对话时调用此函数。这会让机器人在该群组的下一条消息时直接回复，无需@。此效果仅生效一次。
        """
        if not self.config.get('enable_immersive_chat', True):
            return

        group_id = event.get_group_id()
        if group_id:
            logger.info(f"[沉浸式对话] 已为群 {group_id} 开启单次直接回复模式。")
            self.direct_reply_groups.add(group_id)

    # -----------------------------------------------------
    # Feature 2: 主动插话 (Proactive Interjection)
    # -----------------------------------------------------

    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        """机器人发送消息后，启动主动插话的计时器"""
        if not self.config.get('enable_plugin', True) or not self.config.get('enable_proactive_reply', True):
            return
        if event.is_private_chat():
            return
        group_id = event.get_group_id()
        if not group_id:
            return

        # 如果已存在计时器，先取消旧的
        if group_id in self.active_timers:
            self.active_timers[group_id].cancel()
            logger.debug(f"[主动插话] 取消了群 {group_id} 的旧计时器。")

        # 清空历史缓冲区并启动新任务
        self.group_chat_buffer[group_id].clear()
        task = asyncio.create_task(self._proactive_check_task(group_id, event.unified_msg_origin))
        self.active_timers[group_id] = task
        logger.info(f"[主动插话] 机器人发言，已为群 {group_id} 启动主动插话计时器。")

    async def _proactive_check_task(self, group_id: str, unified_msg_origin: str):
        """计时器到点后执行的检查任务"""
        try:
            delay = self.config.get('proactive_reply_delay', 8)
            await asyncio.sleep(delay)

            # 从active_timers中移除自身，表示计时已结束
            self.active_timers.pop(group_id, None)
            chat_history = self.group_chat_buffer.pop(group_id, [])
            
            if not chat_history:
                logger.info(f"[主动插话] 群 {group_id} 在 {delay}s 内无新消息，不进行判断。")
                return

            logger.info(f"[主动插话] 群 {group_id} 计时结束，收集到 {len(chat_history)} 条消息，开始请求LLM判断。")
            
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
                return
                
            llm_response = await provider.text_chat(prompt=prompt)
            
            json_string = ""
            try:
                json_string = self._extract_json_from_text(llm_response.completion_text)
                
                if not json_string:
                    logger.warning(f"[主动插话] 从LLM回复中未能提取出有效内容。原始回复: {llm_response.completion_text}")
                    return

                # 添加更严格的JSON格式检查
                if not json_string.startswith('{') or not json_string.endswith('}'):
                    logger.warning(f"[主动插话] 提取的内容不是有效的JSON对象格式。内容: {json_string}")
                    return

                decision_data = json.loads(json_string)
                should_reply = decision_data.get("should_reply", False)
                content = decision_data.get("content", "")

                if should_reply and content:
                    logger.info(f"[主动插话] LLM判断需要回复，内容: {content[:50]}...")
                    # 使用 from astrbot.api.event import MessageChain 也是可以的
                    message_chain = text=content
                    await self.context.send_message(unified_msg_origin, message_chain)
                else:
                    logger.info("[主动插话] LLM判断无需回复。")

            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                logger.error(
                    f"[主动插话] 解析LLM的JSON回复失败: {e}\n"
                    f"原始回复: {llm_response.completion_text}\n"
                    f"清理后尝试解析的文本: '{json_string}'"
                )

        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的任务被取消。")
        except asyncio.TimeoutError:
            logger.warning(f"[主动插话] 群 {group_id} 的LLM请求超时。")
        except Exception as e:
            logger.error(f"[主动插话] 任务执行出现未知异常: {e}", exc_info=True)
        finally:
            # 确保无论成功还是失败，都清理相关资源
            self.active_timers.pop(group_id, None)
            self.group_chat_buffer.pop(group_id, None)

    # -----------------------------------------------------
    # 统一的消息监听器
    # -----------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """统一处理所有群聊消息"""
        if not self.config.get('enable_plugin', True):
            return

        group_id = event.get_group_id()
        # 忽略机器人自身的消息
        if event.get_sender_id() == event.get_self_id():
            return

        # 逻辑1: 检查是否处于沉浸式对话模式
        if self.config.get('enable_immersive_chat', True) and group_id in self.direct_reply_groups:
            logger.info(f"[沉浸式对话] 检测到群 {group_id} 的直接回复消息，触发LLM。")
            self.direct_reply_groups.remove(group_id) # 仅生效一次
            event.stop_event() # 阻止默认的LLM调用流程，使用我们自己的请求
            yield event.request_llm(prompt=event.message_str)
            return # 处理完毕，直接返回

        # 逻辑2: 如果有主动插话计时器在运行，则记录消息
        if self.config.get('enable_proactive_reply', True) and group_id in self.active_timers:
            sender_name = event.get_sender_name() or event.get_sender_id()
            message_text = event.message_str.strip()
            if message_text:
                # 速率限制，防止短时间消息过多刷爆缓冲区
                if len(self.group_chat_buffer[group_id]) > 20:
                    logger.info(f"群 {group_id} 消息缓冲已达上限，跳过处理")
                    return
                self.group_chat_buffer[group_id].append(f"{sender_name}: {message_text}")

    # -----------------------------------------------------
    # 插件卸载时的清理工作
    # -----------------------------------------------------
    async def terminate(self):
        """插件被卸载/停用时调用，用于清理"""
        logger.info("正在卸载ReplyDirectly插件，取消所有计时器...")
        for task in self.active_timers.values():
            task.cancel()
        self.active_timers.clear()
        self.group_chat_buffer.clear()
        self.direct_reply_groups.clear()
        logger.info("ReplyDirectly插件所有后台任务已清理。")
