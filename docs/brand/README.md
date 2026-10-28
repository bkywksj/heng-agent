# heng-agent 品牌资源

| 文件 | 是什么 | 用在哪 |
|---|---|---|
| `logo.svg` | 圆角方块底 + 白色 H›（青 → 蓝渐变底） | 网站、README、文档站页头、应用图标 |
| `logo-512.png` / `logo-256.png` | `logo.svg` 导出的透明底位图 | 不支持 SVG 的地方（社交头像、商店图） |
| `favicon.ico` | 16 / 32 / 48 / 64 多尺寸 | 网页标签页图标 |
| `mark.svg` | 不带底框的 H›（图形本身是渐变） | 和其他工具并排的图标列表，如智码的工具切换条 |
| 终端字标 | 两行方块字「HENG」，常量 `LOGO`（`src/heng_agent/ui/repl.py`） | 欢迎页；正式版青色、开发版品红 |

## 颜色

| 名字 | 色值 | 说明 |
|---|---|---|
| 青 | `#22D3EE` | 渐变起点；终端里的品牌色（cyan） |
| 蓝 | `#2563EB` | 渐变终点；网页上的主色（白底上做文字也够对比度） |
| 品红 | 终端 magenta | 只用于开发版，和正式版一眼区分 |

渐变方向一律左上 → 右下。H 的横杠是命令行提示符「›」：既是首字母，也说明这是终端工具。

## 在智码（tauri-cc）里用

智码的工具切换条用的是不带底框的裸标（`src/components/icons/providerLogos.tsx`），heng-agent 对应 `mark.svg`。
照那个文件的写法加一个组件：

```tsx
/**
 * heng-agent Logo —— 两竖 + 中间命令行提示符「›」（H 的横杠换成提示符），青 → 蓝渐变。
 * 与 heng-agent 仓库 docs/brand/mark.svg 同一图形；网站 logo 是它加上渐变圆角底。
 * 🔴 渐变 id 用 useId 生成：同一页面会同时渲染多个（工具条 + 设置侧栏），写死 id 会互相串。
 * mono 时丢掉渐变走 currentColor（移动端「品牌色块 + 白色图形」徽章，块的颜色用 #22D3EE → #2563EB 渐变）。
 */
export function HengLogo({ size = 16, mono = false }: ProviderLogoProps) {
  const gid = `heng-grad-${useId()}`;
  const paint = mono ? "currentColor" : `url(#${gid})`;
  return (
    <svg width={size} height={size} viewBox={VIEW_BOX.heng} fill="none" xmlns={XMLNS} style={SVG_STYLE}>
      {!mono && (
        <defs>
          <linearGradient id={gid} x1="2" y1="3" x2="22" y2="21" gradientUnits="userSpaceOnUse">
            <stop stopColor="#22D3EE" />
            <stop offset="1" stopColor="#2563EB" />
          </linearGradient>
        </defs>
      )}
      <rect x="2" y="3" width="4.6" height="18" rx="1.2" fill={paint} />
      <rect x="17.4" y="3" width="4.6" height="18" rx="1.2" fill={paint} />
      <path d="M9.3 7.6L14.4 12L9.3 16.4" stroke={paint} strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
```

`VIEW_BOX` 里加一行（包围盒 `2 3 20 18` 是浏览器 `getBBox()` 实测值；两根实心竖条墨水集中，按实心块一档收到 0.86）：

```ts
  // 两根实心竖条 + 中间提示符：外轮廓接近方块，按实心块一档收
  heng: opticalViewBox(2, 3, 20, 18, 0.86),
```

`ProviderLogo` 的 `switch` 里加 `case "<heng-agent 的 provider id>": return <HengLogo size={size} mono={mono} />;`，
id 要与智码后端 `CliProvider::id()` 一致（智码还没有 heng-agent 这个 provider，加的时候一起定）。
