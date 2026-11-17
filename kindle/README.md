# Kindle 值班看板

`/kindle` 是一个给电子墨水屏用的极简页面：白底黑字、无 JavaScript、无外部资源，每 5 分钟自动刷新。

## 非越狱 Kindle（推荐先试）

1. Mac 和 Kindle 连同一个 Wi-Fi。
2. 查 Mac 的局域网 IP：

   ```bash
   ipconfig getifaddr en0
   ```

3. 启动值班台：

   ```bash
   python3 ops_center.py serve
   ```

4. Kindle 打开「体验版网页浏览器」，访问：

   ```
   http://<Mac-IP>:8890/kindle
   ```

5. 加入书签，放在桌上常亮。页面每 5 分钟刷新一次；Kindle 休眠后需要点一下刷新。

## 越狱 Kindle（可选）

越狱后可以用 KUAL 定时抓取页面/截图并调用 `eips` 刷新，或复用你已有的 `kindle-mac-panel` 的刷新逻辑。`/kindle` 本身是纯 HTML，抓取后需要转成图片才能用 `eips` 显示。

## 限制

- 电子墨水刷新慢，不适合做实时告警器；它是“抬头看一眼”的值班看板。
- Kindle 浏览器较老，所以页面不使用 JavaScript 和外部 CSS。
- Wi-Fi 休眠会停止自动刷新；需要保持常亮或手动刷新。
- 只开在局域网，不要把 8890 暴露到公网。
- 如果页面上是真实资产，Kindle 放在桌面上也要注意可见范围；对外演示用脱敏数据。
