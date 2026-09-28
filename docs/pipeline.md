# Nebula 壁纸视觉管线 —— E2E 提炼

从 Nebula 终端源码提炼的可复现图片处理规范。目标是：**离线烘焙一张背景图**，使任何支持
自定义背景的终端获得与 Nebula 相同的观感，不需要逐软件调参。

源出处：

- `nebula_settings/src/lib.rs` — 五个持久化键与默认值
- `nebula_app/src/renderer/image_layout.rs` — fit/alignment 单一权威实现
- `nebula_app/src/gpui_shell/wallpaper/image_loader.rs` — 有界解码
- `nebula_app/src/gpui_shell/wallpaper.rs` — 调度、合成、模糊、scrim
- `nebula_app/res/image.f.glsl` — 旧壳圆角 SDF 裁剪
- `nebula_app/src/display/surface_opacity.rs` — 透明度策略（modal 保底 0.92）

## 0. 参数面（settings 层）

| 键 | 语义 | 默认 |
|---|---|---|
| `background_image` | 源图路径 | 空=无 |
| `background_image_opacity` | 图自身不透明度 0..1 | **0.38** |
| `background_image_fit` | `fill` / `uniform` / `uniform_to_fill` / `none` | `uniform_to_fill` |
| `background_image_alignment` | 九宫锚点 | `center` |
| `background_image_cover_chrome` | 壁纸是否铺满整窗 | `false` |
| `opacity` | 窗口/壳面不透明度 | `1.0` |
| `blur` | 系统材质 `none/mica/mica-alt/aero/acrylic` | `none` |

## 1. 加载（有界、串行、可取消）

```
文件 ≤ 64 MiB  →  流式格式探测（不整读）
→ 解码限额：单边 ≤ 32768，解码/RGBA 缓冲 ≤ 128 MiB
→ 驻留预算：RGBA ≤ 8 MiB 且 最长边 ≤ 2048
   scale = min(2048/最长边, √(8MiB / (w·h·4)), 1)
→ 先整数面积缩略，再转 RGBA8（16-bit 输入同样先缩）
→ 换序到渲染器原生通道（GPUI = BGRA 预乘）
```

另有独立的**布局尺寸**：`layout = 原图 × min(2560/最长边, 1)`——与纹理分辨率解耦，
`fit=none` 的"原生大小"不因纹理被压缩而变小。

运行时只有**一个加载任务**；`generation` 版本号在解码前/解码中/发布前三处取消
过期工作；`(mtime, size)` stamp 命中则跳过重载；旧纹理显式退役（防止 GPU atlas
泄漏——这是实测出来的坑：GPUI 不随 `Arc` drop 回收 image id）。

## 2. 布局

```
fit:  Fill          draw = (W, H)          # 变形铺满
      Uniform       draw = img × min(W/w, H/h)   # 留白
      UniformToFill draw = img × max(W/w, H/h)   # 裁剪溢出（默认）
      None          draw = (layout_w, layout_h)  # 原生尺寸
原点: x0 = (W − draw_w) × fx,  y0 = (H − draw_h) × fy
      (fx, fy) ∈ {0, 0.5, 1}² 来自九宫锚点
```

x/y 可以为负——cover 靠"画出去再被裁"实现。渲染端只需要一次带裁剪的贴图。

## 3. 合成（自下而上）

```
L0  系统材质   窗口级模糊，画在全部内容之下（见 §4，烘焙不覆盖）
L1  底板       主题背景色（本工具恒为不透明）
L2  壁纸       拟合后的图 × image_opacity 叠在底板上
              —— 透明度是层属性，不烘进像素
L3  scrim      仅 cover_chrome：壳面颜色 × min(window_opacity, 0.78)
L4  内容       终端默认背景格不绘制 → 透出 L2
              文字/彩色单元背景不透明 → 对比度不塌
```

- **card 模式**：壁纸只盖终端卡，四角继承卡片圆角（只有与卡共享的角带半径，
  内部 letterbox 边保持直角）。
- **cover 模式**：壁纸锚定整个 viewport，壳面 scrim（≤0.78）让图在侧栏/标题栏
  下"晕"出来——这就是"毛玻璃感"的主要来源。

像素公式（烘焙版本，自下而上逐层 over）：

```
canvas = bg @ α=1（不透明模式）或 α=0（preserve-alpha）
canvas = img·o_i + canvas·(1 − o_i)           # 图区域内
canvas = scrim·s  + canvas·(1 − s)            # 仅 cover，s = min(o_win, 0.78)
输出 = RGB 压平 / RGBA（preserve-alpha：像素 alpha = 源α × o_i，留白全透）
```

preserve-alpha 是更忠实的跨终端复刻：不透明模式把"向主题底色淡化"烘死成
`img·o_i + --bg·(1−o_i)`；保留 alpha 则由终端将像素叠到**它自己的底色**上，
公式不变但底色自动跟随终端主题——`--bg` 只在 JPEG 压平或终端不支持 alpha 时
才需要。

## 4. 毛玻璃是两条独立的链

| 链 | 落点 | 能否烘进图片 |
|---|---|---|
| 窗口级材质 | DWM system backdrop / AccentPolicy / CGS / KWin blur | 不能——它模糊的是窗口**后方**实时内容 |
| 图片自身 glass 配方 | blur + 白 tint（暗色 0.08 / 亮色 0.05）+ LCG 噪点（512px tile, α≈5.5%） | 可以——本工具的 `--blur-sigma/--tint-alpha/--noise-alpha` |

噪点细节：LCG `state = state·1664525 + 1013904223`（Numerical Recipes 常数），
取高位 `state >> 24` 得灰度；确定性种子 `0x9E3779B9`；tile 平铺而非整图生成。
**绘制顺序固定：tint 在噪点之下**，否则颗粒被 tint 冲淡。

非 Windows 的 `blur.enabled()` → `WindowBackgroundAppearance::Blurred` →
macOS `CGSSetWindowBackgroundBlurRadius(80)` / Wayland KWin blur。

## 5. 工程约束（源码里的实测教训）

1. **透明度不烘像素**——做成层属性，滑块拖拽零成本；烘焙路径只为离线产物。
2. **双分辨率解耦**——纹理按显存预算缩（2048/8MiB），布局按观感预算缩（2560）。
3. **串行 + generation 取消 + stamp 缓存**——单调版本号杜绝旧结果覆盖新状态。
4. **每像素只受一次 alpha**——壳色带互不重叠、先铺凹角再画卡凸角，消除
   `i·(1−i)·clear` 交叉项的四角亮线。
5. **cover→开要确认**——壳面变半透明会降低控件对比度（交互保护，烘焙无对应物）。

## 6. 复刻到任意终端的推荐流程

```
vitrify -i 原图 -o out.png --preserve-alpha      # 推荐：原图分辨率+alpha自适应
vitrify -i 原图 -o out.png --bg "<终端主题底色>"  # 保守：不透明烘焙
# 要裁到终端比例：--aspect 16:9；要像素级画布：--size WxH
# 想要"磨砂图"观感：--preset nebula-glass（图自身模糊+噪点，与窗口模糊无关）
```
