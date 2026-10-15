# 京东官方扫码登录进度

检查日期：2026-10-14，用户时区 America/Managua（UTC−06:00）。

## 已验证的结果

- 已通过继承的环境代理访问官方地址 <https://passport.jd.com/new/login.aspx>，HTTP 200。
- Chromium 加载的页面标题为「京东-欢迎登录」，包含「京东APP扫码登录」，但扫码区域显示「服务器出错」。
- 页面脚本、样式和图片的多个请求返回 `net::ERR_TUNNEL_CONNECTION_FAILED`。二维码图片元素未加载（自然宽高均为 0），所以目前没有可供扫描的有效二维码，也未建立已登录会话。
- 环境状态列出 14 个自定义允许域名、修订号 2、观察当前，但网络规则状态仍为 `unknown`；此状态不能证明全部域名实际可访问。

## 需要补充的实际请求域名

当前可用工具只能读取云环境配置。请在 shop 云环境的允许域名中补充下列登录页实际请求且失败的域名，并发布配置：

```text
storage.jd.com
misc.360buyimg.com
img13.360buyimg.com
storage.360buyimg.com
jcap.m.jd.com
ivs.jd.com
jrsecstatic.jdpay.com
gias.jd.com
seq.jd.com
sgm-static.jd.com
```

资源放行后重新加载官方登录页；如果页面随后请求其他扫码接口，再根据实际请求核对所需域名。成功生成二维码后须保留同一浏览器会话供用户扫码，并以页面返回的登录状态确认结果。

## 会话与证据

本轮未登录页面截图临时保存在 `/tmp/shop-jd-session/login.png`，浏览器辅助程序为 `/tmp/shop_jd_browser.py`。这些临时文件不保证跨实例保留。未把 Cookie、令牌、账号信息或二维码写入仓库。

本轮还请求在 Codex 浏览器面板打开官方登录地址；工具返回 `queued`，不能据此断言用户面板已经显示页面或取得登录状态。
