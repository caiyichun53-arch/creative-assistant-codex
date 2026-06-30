@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   逆向拆 DNA (Codex) -- 出门/睡前双击启动
echo   * 一条一条拆,随时可关窗口
echo   * 撞 LLM 额度/会话上限会自动停,记录断点
echo   * 下次双击从断点续,拆过的永不重拆
echo ============================================
echo.
python scripts\reverse\dna.py --status
echo.
echo --- 开始拆(Ctrl+C 或关窗口=停)---
python scripts\reverse\dna.py --batch
echo.
echo ====== 本轮结束(完成 / 撞限 / 你关闭)======
echo 进度记录在 logs\dna_batch.log(回来看这个);看进度: python scripts\reverse\dna.py --status
echo 全部拆完后再做 reduce 归并共性: python scripts\reverse\reduce.py --batch
pause
