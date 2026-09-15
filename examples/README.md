# 虚构商品演示

HY-17 是专门为软件验证生成的虚构三格收纳盒，没有对应真实商品或店铺。所有图片由本轮 Codex 内置 image_gen 生成／编辑，不可用于证明真实材质、颜色还原率或实际销量提升。

## 文件

- synthetic-product.png：生成的普通桌面参考照片。
- native-packshot.png：基于参考的白底候选。
- native-scene-v1.png：首次场景候选，额外加入了未要求的绿植、笔筒与挂画，记录为需修改。
- native-scene-v2.png：针对性移除上述道具的修正版。
- native-prompts.json：本轮实际使用的制作要求。
- recorded-visual-checks.json：本轮对这些固定图片的可见内容核对，不适用于其他图片。
- provenance.json：样本哈希、来源类型与验证范围。
- request.json：示例本地任务请求。
- delivery/index.html：原图与最终候选对照。
- delivery/manifest.json：交付清单，候选均未被标记为真实卖家选用。

replay_sample.py 只在全部样本哈希匹配后复用这份已记录的判断，回放本地状态与交付。不重新生图、不进行新的视觉判断；换图片后必须重新实际审核。

平台使用范围没有验证。本例“白底”是视觉制作目标，未作完整背景像素或平台审核判定。

