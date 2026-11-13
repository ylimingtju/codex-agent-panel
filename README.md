# Codex Agent Panel

Codex Agent Panel 在 Codex 桌面端的聊天里显示任务进度卡。点击 **在右侧打开**，可以一边继续聊天，一边查看各子代理的任务、已报告步骤和耗时。面板使用本机 Codex 自带的 Codey 动画表示工作、待复核和已验收等状态。

总进度按主代理已验收的计划项计数。子代理返回结果后进入待复核，主代理确认通过才增加计数。没有步骤数据时，面板显示“步骤未报告”。

## 使用

插件启用后，在聊天中输入“显示子代理进度”即可打开进度卡，也可以让 Codex 在派出子代理时开始跟踪。MCP 服务使用 Python 3 标准库。macOS 版在运行时从本机应用读取 Codey 美术资源。

GitHub 项目名为 `codex-agent-panel`。Codex 中的安装 ID 沿用 `savvy-progress-codex`，以兼容已有聊天卡片。
