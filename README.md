# 万圣节 10.30–10.31 库存消耗监控

每小时自动拉取 Superset 看板 27（散客票务监测）数据，计算两晚分票型库存消耗 / 卖速 / 售罄预测，
推送飞书群卡片，并把完整报告发布到 GitHub Pages。

## 口径
- 已售 = 支付成功票数（order_status=2，已扣退款成功）
- 库存：超级早鸟 300 / 早场 900 / 正价 1,050，每晚独立
- 嘉年华会员专属并入正价票
- 剔除 City Drink、限时参团、员工福利场次票

## 部署（一次性，约 5 分钟）
1. 在 GitHub 新建公开仓库 `halloween-monitor`，把本目录推上去
2. 仓库 Settings → Secrets and variables → Actions → 新建 `FEISHU_WEBHOOK`，值为飞书自定义机器人 Webhook
3. 仓库 Settings → Pages → Source 选 `gh-pages` 分支 / root
4. 第一次手动触发：Actions → Halloween Ticket Monitor → Run workflow
5. 之后每小时自动运行（约每小时第 7 分钟）

## 飞书机器人创建
群设置 → 机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook 地址
