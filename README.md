# GitHub 资料卡验证

在[在线验证页面](https://xusu616.github.io/try_to_see_the_banned_account/)输入 GitHub 用户名，确认一张已填写的 GitHub 请求单，页面便会自动等待验证结果。

后台会在现有 `v0.1.1` 发布说明里加入该用户名的 `@提及`，通过正常的公开页面交互展开贡献者资料卡，再把读取到的头像、名称、简介及 PRO 标记返回到网页。结果保留来源链接和记录时间。

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

1. 输入 GitHub 用户名，点击开始验证。
2. 在打开的 GitHub 页面点击提交请求，返回网站等待。
3. 工作流按顺序更新同一个 Release，并读取对应贡献者的公开卡片。
4. 验证结果显示后，可分享请求结果的链接。

GitHub Actions 排队和页面更新需要时间，页面会持续检查进度。限流、网络失败或资料卡暂未出现时，页面会显示实际状态。后台只接受格式有效的用户名和请求编号，修改固定仓库的现有 Release，结果评论由 `github-actions[bot]` 发布。

## 本地构建

```powershell
python scripts/build_pages.py --output dist
python -m http.server 8765 --bind 127.0.0.1 --directory dist
```

访问 `http://127.0.0.1:8765/` 即可预览。`python scripts/package_tool.py --output dist-tool` 会生成工具与 SHA-256 校验文件。推送版本 Tag 后，Release 工作流会重新打包并发布对应版本。
