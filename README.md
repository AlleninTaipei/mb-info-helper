# ASUS 主機板更新追蹤

定期查詢 ASUS ROG 公開 API, 同時追蹤多張主機板的 BIOS、PD Firmware、Intel ME 與 CPU QVL。資料與前一次保存在 `data/state.json` 的結果比較, 有異動時透過 SMTP 寄送摘要郵件。

目前監控型號：

- AM5：`ROG STRIX X870E-E GAMING WIFI7 NEO`。
- LGA1851：`ROG STRIX Z890-A GAMING WIFI`。

## 執行方式

本機測試查詢, 不寄信也不更新狀態：

```bash
python3 -m pip install -r requirements.txt
python monitor.py --dry-run
```

正式執行：

```bash
python monitor.py
```

第一次正式執行只會建立基準資料, 不會寄信。之後只有資料變化時才會寄信並更新狀態檔。唯一的第三方套件是 `certifi`, 用於跨平台 TLS 憑證驗證。

## GitHub Actions 設定

建立 GitHub repository 並推送本專案後, 到 `Settings > Secrets and variables > Actions` 建立以下 Repository secrets：

| Secret | 說明 | Gmail 範例 |
|---|---|---|
| `SMTP_HOST` | SMTP 主機 | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP 連接埠 | `587` |
| `SMTP_USERNAME` | SMTP 帳號 | Gmail 地址 |
| `SMTP_PASSWORD` | SMTP 密碼 | Google 應用程式密碼 |
| `MAIL_TO` | 收件地址 | 你的信箱 |
| `MAIL_FROM` | 寄件地址, 可省略 | 預設同 SMTP 帳號 |
| `SMTP_STARTTLS` | 是否使用 STARTTLS, 可省略 | 預設 `true` |

不要將 SMTP 密碼直接寫入 repository。Gmail 需啟用兩步驟驗證並建立應用程式密碼, 不使用一般登入密碼。

Workflow 預設每天在 UTC `01:17` 與 `13:17` 執行, 即台北時間 `09:17` 與 `21:17`。也能在 Actions 頁面使用 `Run workflow` 手動執行。

### 驗證 SMTP 寄信

SMTP 測試郵件功能已整合至 GitHub Actions 的手動執行介面。Secrets 設定完成後：

1. 開啟 repository 的 `Actions`。
2. 選擇 `Monitor ASUS motherboard updates`。
3. 點選 `Run workflow`。
4. 勾選 `Send a test email without changing state`。
5. 再點選綠色的 `Run workflow`。

成功時, `Check ASUS updates` 步驟會顯示 `Test email sent to ...`, 收件信箱會收到主旨為 `[ASUS 監控] SMTP 測試成功` 的郵件。測試模式不查詢 ASUS API, 也不修改 `data/state.json`。若未收到郵件, 請一併檢查公司信箱的垃圾郵件匣。

本機也可以使用相同的環境變數執行：

```bash
python monitor.py --test-email
```

## 變更偵測與郵件觸發條件

每張主機板的 BIOS、PD Firmware、Intel ME 與 CPU QVL 都是獨立比較項目。任何機種或分類發生變化都會寄送郵件；如果多個項目在同一次執行中同時變化, 只會寄送一封郵件, 並按照 Socket、主機板型號及資料分類顯示摘要。

BIOS 的觸發條件：

- 新增 BIOS 版本。
- 移除既有 BIOS 版本。
- 相同版本的發布日期、正式版或 Beta 狀態、檔案大小、更新說明、SHA-256 或下載路徑改變。

PD Firmware 與 Intel ME 的觸發條件：

- 新增或移除版本。
- 相同工具與版本的發布日期、檔案大小、更新說明、SHA-256 或下載路徑改變。

CPU QVL 的觸發條件：

- 新增 CPU。
- 移除既有 CPU。
- 相同 CPU 的 PCB 版本、最低 BIOS 需求或備註改變。

沒有任何變化時, workflow 只會顯示：

```text
No changes detected.
```

此時不會連線 SMTP、不會寄信, 也不會修改 `data/state.json`。

### 郵件摘要範例

新增 BIOS 時：

```text
主旨: [ASUS 更新] ROG STRIX X870E-E GAMING WIFI7 NEO

ASUS 主機板監控偵測到更新
型號: ROG STRIX X870E-E GAMING WIFI7 NEO
時間: 2026-07-18 01:17 UTC

[BIOS]
- 新增: 1003 (2026/07/18, 正式版)

產品頁: https://rog.asus.com/tw/motherboards/rog-strix/rog-strix-x870e-e-gaming-wifi7-neo/
```

CPU QVL 的最低 BIOS 需求改變時：

```text
[CPU QVL]
- 變更: Ryzen 7 7700X3D (...)
  bios_version: 0916 -> 1003
```

目前新增 BIOS 的摘要會列出版本、日期與正式版或 Beta 狀態。相同版本的欄位被 ASUS 修改時, 郵件會列出變更前後的內容。

## 新增或更換主機板

在 `config.json` 的 `products` 陣列新增 ASUS ROG 產品或支援頁網址：

```json
{
  "products": [
    {
      "name": "ROG STRIX X870E-E GAMING WIFI7 NEO",
      "socket": "AM5",
      "product_url": "https://rog.asus.com/tw/motherboards/rog-strix/rog-strix-x870e-e-gaming-wifi7-neo/"
    },
    {
      "name": "ROG STRIX Z890-A GAMING WIFI",
      "socket": "LGA1851",
      "product_url": "https://rog.asus.com/tw/motherboards/rog-strix/rog-strix-z890-a-gaming-wifi/"
    }
  ]
}
```

腳本會透過路由 API 自動取得每個產品的 `m1Id` 與 `levelTagId`, 不需手動填寫產品識別碼。新增機種後第一次執行只會建立該機種基準, 不會把既有的歷史版本當成更新寄信。移除設定中的機種也只會更新基準, 不會誤報為 ASUS 移除全部資料。

## 失敗行為

API 或寄信失敗時, 程式以非零狀態結束, GitHub Actions 會標示失敗。寄信成功後才更新狀態檔, 避免郵件傳送失敗卻遺失該次異動通知。

## API 探索方式

本專案使用的 API 是從 ASUS 官網載入的 JavaScript 與網路請求邏輯分析得知, 並非來自 ASUS 開發者文件。探索流程如下。

### 1. 從轉存網頁確認資料來源

最初取得的網頁轉存包含：

- HTML。
- ASUS 前端 JavaScript。
- 頁面載入後產生的 BIOS 資料。

在這些檔案中搜尋：

```text
productSupportBIOS
helpdesk_bios
GetPDBIOS
ProductV2
```

接著在 ASUS 壓縮過的 JavaScript 中找到 `getProductSupportBIOS` 呼叫函式。此函式實際呼叫：

```text
/support/webapi/ProductV2/GetPDBIOS
```

並傳入以下參數：

```text
website
model
pdid
m1id
cpu
LevelTagId
```

### 2. 找出產品識別碼來源

BIOS API 需要 `m1Id` 與 `LevelTagId`, 只使用產品名稱不足以取得資料。繼續檢查 JavaScript 後, 找到產品頁初始化時會呼叫：

```text
https://api-rog.asus.com/recent-data/api/v3/Route
```

傳入產品頁路徑：

```text
WebURL=tw/motherboards/rog-strix/rog-strix-x870e-e-gaming-wifi7-neo/
```

回應中包含：

```json
{
  "websitePath": "tw",
  "webPathName": "rog-strix-x870e-e-gaming-wifi7-neo",
  "m1Id": 34707,
  "levelTagId": 246662
}
```

將這些識別資料傳給 `GetPDBIOS`, 即可取得完整的 BIOS JSON。

### 3. 找到 CPU QVL API

同一份 JavaScript 中還有 `getProductSupportCPU` 函式, 它呼叫：

```text
/support/webapi/ProductV2/GetPDCPUList
```

所需參數與 BIOS API 接近：

```text
website
model
pdid
m1id
mode
LevelTagId
```

實際測試後可成功取得 CPU QVL。

### 4. 分析 BIOS 回應分類

測試 Z890 主機板時發現, `GetPDBIOS` 不只回傳 BIOS, 還可能包含：

- BIOS。
- 韌體。
- Intel ME。

因此程式依照 API 回傳的 `Name` 分類：

```text
BIOS     -> bios
韌體     -> firmware
Intel ME -> intel_me
```

這可避免將所有檔案都視為 BIOS。

### 5. 使用實際請求驗證

透過公開的 HTTP GET 請求測試各端點後, 確認：

- 不需要登入。
- 不需要 API Key。
- 參數可重現官網資料。
- JSON 結構與網頁顯示內容一致。
- AM5 與 Intel Z890 主機板均可使用。

整體方法可概括為：

```text
觀察官網行為
-> 搜尋前端 JavaScript
-> 找到 API 端點
-> 找出參數來源
-> 重現 HTTP 請求
-> 對照網頁資料
-> 編寫自動化程式
```

這屬於對公開網站前端行為的技術分析。由於這些端點沒有正式的第三方文件或穩定性承諾, 專案需保留 API 失敗保護與資料結構變更的風險說明。
