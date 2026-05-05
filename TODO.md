# 待办清单 (Backlog)

- [x] **自动清理低互动的主动推文（Delete Low-Engagement Posts）**
  - 在 `revisit.py` 的 24 小时回访逻辑中增加阈值判断。
  - 对于互动极低（如 0 赞 0 回复且低曝光）的主动发推，利用浏览器自动化将其删除，以保持个人主页内容质量。

- [ ] **支持带图发推（Image Generation / Attached Media）**
  - 扩展主动发推 `post_once.py` 的功能。
  - 在生成特定类型（如 `story` 或 `casual`）的推文时，配合附带图片或由生图工具生成的配图上传，增加推文曝光率。
