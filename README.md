# GitHub 资料卡存档

批量查找公开 GitHub Tag、Issue、PR 和提交页面里还能展开资料卡的署名。站点用提交到仓库的公开资料重绘卡片，并保留每条记录的来源链接。

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

## 添加一张卡片

打开 [GitHub Pages](https://xusu616.github.io/try_to_see_the_banned_account/) 的提交表单，填写任意公开仓库的 GitHub Release 或 Tag 链接和页面里能看到的资料。正式 Release 可自动读取署名和头像。只有 Git Tag 时，填写页面上显示的署名，构建会尝试从同一条署名读取头像。头像地址可以留空，查不到时会显示用户名首字母。

预览确认后，表单会打开 GitHub 编辑器。保存到分支并创建 PR，便可在 Actions 中查看校验结果或下载 `pages-preview`。合并后站点会自动更新。

也可以直接在 `cards/` 添加一份 JSON。文件名使用小写字母、数字、点和连字符。同一来源 URL 只需一条记录。[示例数据](cards/rd227-v1.1.0-ksun-susfs.json)

首次发布时，在 [仓库 Pages 设置](https://github.com/xusu616/try_to_see_the_banned_account/settings/pages) 将构建来源设为 **GitHub Actions**，然后运行 [Pages 工作流](https://github.com/xusu616/try_to_see_the_banned_account/actions/workflows/pages.yml)。

## 本地构建

```powershell
python scripts/build_pages.py --output dist
python -m http.server 8765 --bind 127.0.0.1 --directory dist
```

访问 `http://127.0.0.1:8765/` 即可预览。`python scripts/package_tool.py --output dist-tool` 会生成工具与 SHA-256 校验文件。推送版本 Tag 后，Release 工作流会重新打包并发布对应版本。
