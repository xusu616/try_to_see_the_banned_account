# GitHub 资料卡验证

在[在线验证页面](https://xusu616.github.io/try_to_see_the_banned_account/)输入 GitHub 用户名，先查询已经核验的资料。已收录账号直接显示，无需登录或重复提交；未收录时，点击预填的 GitHub 请求单确认提交，页面自动等待结果。

后台先匿名访问 `https://github.com/<用户名>`，只有原始地址明确返回 HTTP 404 才继续核验资料卡。返回 200 或重定向的账号不会收录，也不会修改 Release；网络失败、403、429 和服务端错误会明确报告核验失败。404 本身不代表账号一定被封禁。

通过主页检查后，再检查现有 `v0.1.1` Release 的 Contributors；已有用户名直接展开卡片，缺失时才在发布说明里加入该用户名的 `@提及`。通过正常的公开页面交互读取头像、名称、简介及 PRO 标记，结果保留来源链接和记录时间。

需要使用 GitHub 登录确认请求。GitHub Pages 托管页面，Actions 使用运行时的仓库权限处理请求。普通访客可以打开已完成的请求链接查看结果。

## 下载和运行

从 [GitHub Releases](https://github.com/xusu616/try_to_see_the_banned_account/releases/latest) 下载 `find-profile-cards.pyz`。需要 Python 3.9 或更新版本，直接运行即可。

```powershell
python -X utf8 .\find-profile-cards.pyz rD227 xusu616/android_kernel_xiaomi_markw-4.9
```

扫描多个仓库时可以继续追加 `owner/repo` 或完整页面链接。`--starred <用户名>` 会加入该用户公开 Star 的仓库，`--max-tags <数量>` 控制每个仓库的检查范围。

```powershell
python -X utf8 .\find-profile-cards.pyz rD227 --starred xusu616 --max-tags 10
```

打开生成的 `profile-card-links.html`，在来源页悬浮对应署名即可查看 GitHub 资料卡。

## 验证流程

1. 输入 GitHub 用户名，点击查询。已收录账号直接显示，也可按需点击“重新核验资料”。
2. 未收录或主动重新核验时，点击“打开 GitHub 提交页面”，确认提交请求后返回网站等待。
3. 工作流先确认主页返回 404，再检查 Release 中已有贡献者，缺失时才更新同一个 Release，然后读取对应公开卡片。
4. 验证结果显示后，可分享请求结果的链接。

搜索框和本次查询状态位于顶栏。下方“英灵殿”只收录 `github-actions[bot]` 明确返回 `verified`、与原查询请求匹配、且主页确认返回 HTTP 404 的结果；同一账号保留最近一次成功核验，每页显示 6 个，可在页面底部切换页码。新结果立即显示，工作流成功后自动更新 Pages 中的公共列表。缺少主页状态的旧结果需要重新核验才能展示。

简介通过 `textContent` 保留原始换行，也会把字面 `\n`、`\r\n` 显示为换行。首次生成索引时会重新读取旧记录对应的已有贡献者卡片，恢复被旧版 `innerText` 压成空格的换行，迁移过程不会修改 Release。

Pages 构建使用 SQLite 的不区分大小写用户名主键和核验时间索引，只保存每个账号的最新成功结果。数据库通过 Actions 缓存复用，后续按同步时间游标增量读取新评论；首次构建或缓存丢失时从公开记录重建。入库时再次检查主页，每批同一账号只检查一次；历史记录缺少 404 证据时也必须通过检查。重新核验发现账号主页不再返回 404 时，会移除对应记录和导出的旧文件。本次规则升级会重建旧数据库缓存。

页面加载只获取小型元数据和当前页 6 张卡片；用户名查询直接读取对应 JSON 文件，已访问页在当前会话复用，不在浏览器扫描全部 Issue 或评论。数据库只在构建时使用，Pages 发布物不包含 SQLite 文件。公开列表保存核验时的状态，账号之后恢复可见时，需重新核验才能更新；不会在每次访问时扫描所有账号。浏览器收到排除结果后会立即隐藏该卡片，数量和分页等待下一次发布更新。

GitHub Actions 排队和页面更新需要时间，页面会持续检查进度。限流、网络失败或资料卡暂未出现时，页面会显示实际状态。后台只接受格式有效的用户名和请求编号，修改固定仓库的现有 Release，结果评论由 `github-actions[bot]` 发布。

## 本地构建

```powershell
python scripts/build_pages.py --output dist
python -m http.server 8765 --bind 127.0.0.1 --directory dist
```

访问 `http://127.0.0.1:8765/` 即可预览。`python scripts/package_tool.py --output dist-tool` 会生成工具与 SHA-256 校验文件。推送版本 Tag 后，Release 工作流会重新打包并发布对应版本。

直接用浏览器打开 `site/index.html` 时，会从已发布网站读取公开账号列表；浏览器无法通过 `file://` 读取相邻的 JSON 文件。通过 HTTP 访问构建目录时，读取该目录的账号数据，本地未同步数据库时显示空列表。

构建后的 HTML 引用带内容指纹的 CSS 和 JS 文件，避免浏览器或 CDN 将新版页面与旧版资源混用。账号列表读取失败时会结束加载并显示重读按钮；重读会同时刷新列表元数据和分页缓存。
