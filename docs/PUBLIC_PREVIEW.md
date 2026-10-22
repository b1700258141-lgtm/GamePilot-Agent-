# 公开预览部署

目标：访客打开一个 HTTPS URL 即可体验手动战斗、连战、离线 Agent 测试、缺陷注入、过程与报告。公开预览不发送真实模型请求：选择真实模型时页面提示“当前版本无权限，请联系平台制作者申请权限。”，服务端对真实运行请求返回 HTTP 403。自定义 skill 只能由真实模型执行，因此公开预览中不提供上传。

## 免费预览方案

- Render Free Web Service 运行已有 Docker 镜像，提供 `onrender.com` HTTPS 地址。
- 默认使用内存仓储，无需银行卡或数据库账户。游戏会话、Agent 运行记录和报告可在当前服务进程中查看；服务休眠或重新部署后这些记录会清空。
- Render 的实例闲置 15 分钟会休眠，首次访问可能约需一分钟唤醒。该配置适合即时试用，不适合依靠历史数据做长期展示。

若需要持久保存记录，可另外创建 Neon Free PostgreSQL 数据库，设置 `REPOSITORY_BACKEND=postgres` 和 `DATABASE_URL`（前缀改为 `postgresql+psycopg://`，保留 SSL 查询参数），并在启动前执行 `python -m alembic upgrade head`。完整连接 URL 只保存在 Render 环境变量中，不写入仓库、截图或日志。Render 免费 PostgreSQL 可能要求银行卡验证，且数据库到期后会失效，不适合作为长期展示库。

免费额度和政策可能变化，以 [Render 免费服务说明](https://render.com/docs/free) 与 [Neon 定价页](https://neon.com/pricing) 为准。本配置适合小流量作品展示，不承诺持续在线或大量并发。Render 的免费实例为单实例，应用仍采用一个进程；离线测试在服务端逐步执行。

## 上线步骤

1. 将此仓库中准备好的改动提交并推送到所有者控制的 Git 仓库。部署可连接私有仓库，不必为了预览公开源代码。
2. 在 Render 使用仓库根目录的 [`render.yaml`](../render.yaml) 创建 Blueprint，或手动创建 Docker Web Service。确认选择 Free 计划、单 worker，并设置 `GAMEPILOT_PUBLIC_PREVIEW=true`。不要传入模型密钥。
3. 首次部署会构建 Docker 镜像并启动 FastAPI。Render 的 `/ready` 健康检查通过后，从服务页面复制 `https://…onrender.com/` 分享给体验者。

部署前不要把本地 `.env` 或本地数据库数据上传。Blueprint 已关闭后续自动部署；代码更新时由所有者在 Render 手动触发部署。

## 上线验收

1. 打开公开 URL，确认页面显示服务就绪。用固定 seed 创建手动战斗，执行药水、重击、魔法、下一场，并刷新页面确认同一进程内可恢复会话。
2. 打开 Agent 自动测试，选择“离线演练”，指定 seed 与缺陷，确认最终判定、过程、无模型重放及原始报告可读。
3. 选择“真实模型测试（需申请权限）”，确认页面立即出现指定提示。直接向 API 提交 `provider_mode=real` 也必须得到 HTTP 403，且不能创建运行记录或发起模型调用。

如果部署失败，先看 Render 的构建与运行日志。免费服务休眠后的首访等待属于平台行为；若需要稳定响应或记录持久保存，应再确认托管与数据库方案。
