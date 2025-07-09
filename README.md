# AstrBot 智能直接回复插件 (Reply Directly)

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3Dv3.4.36-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

一个为 AstrBot 设计的智能回复插件，赋予机器人免@直接回复和对话后反思追问的能力，让对话更加流畅和人性化。

---

## ✨ 功能特性

本插件旨在解决群聊中与机器人高频互动时的两个核心痛点：

1.  **免@直接回复模式 (Direct Reply Mode)**
    - **场景**: 当你和机器人在群里“聊上头”时，频繁地 `@` 机器人会打断对话的流畅性。
    - **解决方案**: LLM 可以通过其函数工具（Function Calling）能力，在判断对话进入“热聊”状态后，主动调用 `enable_direct_reply` 工具。一旦授权，机器人在该群便可以直接回复你的消息，无需你再次 `@` 它。当话题结束时，LLM 也可以自行决定关闭此模式。

2.  **反思追问模式 (Follow-up Mode)**
    - **场景**: 传统的问答机器人通常在回复后就结束了当前回合，缺乏对后续对话的感知。
    - **解决方案**: 在机器人给出回复后，它会“倾听”一小段时间（默认5秒）。然后，它会结合自己刚才的回复和这段时间内的新消息，再次请求 LLM 判断是否需要进行追问、澄清或补充。这使得机器人看起来更像一个真正参与讨论的成员，而不是一个被动的问答工具。

    **💡 示例:**
    > **你**: "今天天气怎么样？"
    >
    > **Bot**: "今天天气晴朗，非常适合出门散步。"
    >
    > *(几秒后...)*
    >
    > **群友A**: "是啊，可惜我下午还要开会，没法出去了。"
    >
    > **Bot (主动追问)**: "开会确实挺辛苦的，希望你的会议一切顺利！"

## 🚀 安装

1.  确保您的 AstrBot 版本 `>= v3.4.36`。
2.  进入 AstrBot 项目的 `data/plugins` 目录。
3.  克隆本仓库到该目录下：
    ```bash
    git clone https://github.com/qa296/astrbot_plugin_reply_directly.git
    ```
4.  返回 AstrBot 的 WebUI，在 `插件管理` 页面找到 `ReplyDirectly` 插件，点击 `重载插件` 以加载。

## 🎮 使用指南

插件安装并启用后，功能将自动生效。

-   **对于“直接回复模式”**:
    1.  像平常一样与机器人聊天（需要 `@` 它）。
    2.  如果 LLM 认为对话足够连贯，它可能会自行决定开启直接回复模式。
    3.  你会收到一条类似 `（已开启与 xxx 的直接畅聊模式！）` 的系统提示。
    4.  之后，你在该群的发言**无需@机器人**，它也会进行响应。

-   **对于“反思追问模式”**:
    -   此功能完全在后台自动运行，您无需进行任何操作。
    -   您会观察到，在某些对话场景下，机器人在回复后可能会根据群内新的聊天内容，主动发出补充或追问的消息。

## 🔧 配置项

您可以在 AstrBot 的 **WebUI -> 插件管理 -> ReplyDirectly -> 管理** 页面中对以下参数进行配置。

| 配置项                   | 类型    | 描述                                                                       | 默认值  |
| ------------------------ | ------- | -------------------------------------------------------------------------- | ------- |
| `enable_direct_reply_mode` | `布尔值`  | 是否启用“直接回复”模式？启用后，LLM才可以使用函数工具授权免@回复。         | `True`  |
| `enable_follow_up_mode`  | `布尔值`  | 是否启用“反思追问”模式？启用后，Bot在回复后会等待并决定是否追问。        | `True`  |
| `follow_up_delay`        | `整数`    | 在Bot回复后，等待多少秒进行“反思追问”的判断。单位是秒。                    | `5`     |

## 💡 实现原理 (供开发者参考)

-   **直接回复模式**:
    1.  通过 `@filter.llm_tool` 注册了 `enable_direct_reply` 和 `disable_direct_reply` 两个函数工具。
    2.  插件内部维护一个 `Set` 集合 `direct_reply_users`，用于存储被授权用户的唯一标识 (`group_id_user_id`)。
    3.  通过一个高优先级的 `@filter.event_message_type(priority=10)` 监听器，在 AstrBot 默认的 LLM 处理器之前拦截所有消息。
    4.  如果消息发送者在授权集合中且没有 `@` 机器人，则插件会调用 `event.request_llm()` 主动将消息喂给 LLM，并使用 `event.stop_event()` 阻止事件继续传播。

-   **反思追问模式**:
    1.  通过 `@filter.on_llm_response` 事件钩子，在机器人每次成功回复后触发。
    2.  为了不阻塞主流程，使用 `asyncio.create_task()` 创建一个后台任务 `_follow_up_task`。
    3.  该任务首先 `await asyncio.sleep()` 等待预设的秒数。
    4.  然后从内存中的消息历史记录里，筛选出这几秒内的新消息。
    5.  构建一个特殊的 Prompt，要求 LLM 判断是否需要基于新消息进行补充，并以固定的 JSON 格式返回结果。
    6.  解析 LLM 返回的 JSON，如果 `should_reply` 为 `true`，则通过 `self.context.send_message()` 将追问内容发送出去。

## 🤝 反馈与贡献

如果您发现任何 Bug 或有功能建议，欢迎在 [GitHub Issues](https://github.com/qa296/astrbot_plugin_reply_directly/issues) 中提出。
也欢迎您通过 Pull Request 为本项目做出贡献！

