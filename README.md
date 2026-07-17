# ASUS 主機板更新追蹤

定期查詢 ASUS ROG 公開 API, 追蹤指定主機板的 BIOS 與 CPU QVL。資料與前一次保存在 `data/state.json` 的結果比較, 有異動時透過 SMTP 寄送摘要郵件。

目前監控型號為 `ROG STRIX X870E-E GAMING WIFI7 NEO`。

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

## 更換主機板

修改 `config.json` 中的 ASUS ROG 產品或支援頁網址即可。腳本會透過路由 API 自動取得 `m1Id` 與 `levelTagId`, 不需手動填寫產品識別碼。

## 失敗行為

API 或寄信失敗時, 程式以非零狀態結束, GitHub Actions 會標示失敗。寄信成功後才更新狀態檔, 避免郵件傳送失敗卻遺失該次異動通知。
