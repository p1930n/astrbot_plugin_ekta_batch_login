# astrbot_plugin_ekta_batch_login

AstrBot 第二课堂活动报名与签到/签退二维码批量处理插件。

## 功能

- `.ekta add` 支持本条消息图片，也支持等待同一用户下一条图片消息。
- 自动识别第二课堂活动页面二维码与签到/签退二维码。
- 后台双队列处理：活动加入任务优先，活动队列清空后再处理签到/签退任务。
- 执行中发送任务开始、失败/跳过账号、任务完成汇总消息。
- 账号从本地 CSV 读取，不从聊天消息接收账号密码。

## 账号 CSV

默认读取：

```text
data/ekta_batch_login/accounts.csv
```

格式：

```csv
code,password
20240001,example_password
```

## 命令

```text
.ekta help
.ekta status
.ekta add [--dry-run]
```

`.ekta add` 当前消息未携带图片时，会等待同一用户的下一条图片消息。

## Node 依赖

执行器需要 Node.js 18 或更高版本。

插件优先使用 `vendor/node_modules`，没有时会回退到现有的 `data/ekta_batch_login/node_modules`。如果独立安装依赖：

```powershell
cd data/plugins/astrbot_plugin_ekta_batch_login/vendor
npm install
```

## 安全边界

- 默认允许 AstrBot 管理员或群管理员使用。
- 不输出密码、Token 或二维码解密明文。
- 活动要求报名材料时跳过，不自动填充材料。
- 未经 dry-run 验证前，建议先开启 `dry_run_by_default`。
