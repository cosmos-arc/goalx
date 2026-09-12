# LLM 编排采用 OpenAI Agents SDK（Python）

scout/gate/analyst 拓扑（票 05）需要的是库式内嵌 FastAPI 的薄编排层：类型化结构输出（Pydantic）、OpenAI 兼容自定义 provider（DeepSeek/GLM 优先）、小模型批量调用。选 OpenAI Agents SDK：轻量、抽象少、生产就绪，自定义 OpenAI 兼容端点为一等支持。

落选记录：DeepSeek Harness（DSH）——README 仅文档化 npx/Node 运行时形态，Python in-process、provider 插件、工具定义均无文档，且开发者预览版明示将有不兼容变更（2026-09-13 验证）；pydantic-ai——用户否决（2026-09-12）；LangGraph——状态机过重且调度耦合付费平台（票 05）；CrewAI/AutoGen——角色团队/会话型范式不匹配；strands/smolagents——开放式少脚手架取向，适合研究型 agent 而非结构化批量管道；Agno——轻量但生态弱。若未来需要开放式研究 agent，可单独引入，不影响本决策。
