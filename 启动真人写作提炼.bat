@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   真人写作提炼 (map) -- 出门/睡前双击启动
echo   * 一块一块慢慢消化,随时可关窗口
echo   * 撞 LLM 额度/会话上限会自动停,记录断点
echo   * 下次双击从断点续,完成的块永不重跑
echo ============================================
echo.
python scripts\humanize\extract.py --status
echo.
echo --- 开始 map(Ctrl+C 或关窗口=停)---
python scripts\humanize\extract.py --map
echo.
echo ====== 本轮结束(完成 / 撞限 / 你关闭)======
echo 进度记录在 logs\humanize_map.log(回来看这个);看进度: python scripts\humanize\extract.py --status
echo 全部 map 完成后,跑 reduce 归并: python scripts\humanize\extract.py --reduce
pause
