# Cloudflare Pages 配置说明

## 项目结构

```
stock_data/                       # 项目根目录
├── frontend/                     # 前端子目录（Cloudflare Pages 构建根目录）
│   ├── package.json               # 包含构建脚本
│   ├── vite.config.ts             # Vite 配置
│   ├── src/                      # 源代码
│   ├── dist/                     # 构建输出（自动生成）
│   └── ...
├── web/                          # 后端 API
├── core/                         # 数据层
├── quant/                        # 量化层
└── ...
```

## Cloudflare Pages 配置

### Root directory
**必须是 `frontend/`**

原因：
- `package.json` 在 `frontend/` 目录下
- Cloudflare Pages 会在指定根目录下查找 `package.json`
- 如果设置 `/`，Cloudflare 会在项目根目录查找，但找不到 `package.json`

### Build command
**必须是 `npm run build`**

原因：
- `frontend/package.json` 中的 scripts：
  ```json
  "scripts": {
    "build": "tsc && vite build"
  }
  ```

### Build output directory
**必须是 `dist`**

原因：
- Vite 默认输出目录是 `dist`（相对于 Root directory）
- 即 `frontend/dist/`
- 包含构建产物：`index.html`, `assets/`, `favicon.svg`

## 完整配置

| 配置项 | 值 | 说明 |
|--------|-----|------|
| Framework preset | `Vite` | 框架预设 |
| Build command | `npm run build` | 构建命令（在 frontend/ 下执行）|
| Build output directory | `dist` | 输出目录（相对于 frontend/）|
| Root directory | `frontend` | 构建根目录 |

## 构建流程

1. Cloudflare Pages 切换到 `frontend/` 目录
2. 执行 `npm install`（安装依赖）
3. 执行 `npm run build`（构建前端）
4. 将 `frontend/dist/` 部署到 CDN

## 常见错误

### 错误 1：Root directory 设置为 `/`
```
Error: Could not find a package.json file in the root directory
```
**解决**：改为 `frontend/`

### 错误 2：Build output directory 设置为 `frontend/dist`
```
Error: Build output directory is invalid
```
**解决**：改为 `dist`（相对于 Root directory）

### 错误 3：Build command 设置为 `cd frontend && npm run build`
```
Error: Build command failed
```
**解决**：改为 `npm run build`（Cloudflare 会自动在 Root directory 下执行）
