name: Hammer Alert

on:
  schedule:
    - cron: '*/15 * * * *'   # chạy mỗi 15 phút — GitHub có thể trễ vài phút vào giờ cao điểm
  workflow_dispatch: {}       # cho phép bấm chạy thử thủ công trong tab Actions

permissions:
  contents: write            # cần quyền ghi để lưu lại lịch sử đã báo (tránh báo trùng)

jobs:
  check-hammer:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Chạy kiểm tra mô hình nến
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          SYMBOLS: BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,NEARUSDT
          INTERVALS: 1h,4h,1d
        run: python check_hammer.py

      - name: Lưu lại trạng thái đã báo (tránh báo trùng)
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add alerted.json
          git diff --cached --quiet || git commit -m "cập nhật trạng thái cảnh báo"
          git push
