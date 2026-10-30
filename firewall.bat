@echo off
rem Run once (right-click > "Run as administrator").
rem Allows MyWhoosh on other devices (phone / Apple TV) to reach this bridge.
netsh advfirewall firewall delete rule name="ClickBridge OBC" >nul 2>&1
netsh advfirewall firewall add rule name="ClickBridge OBC" dir=in action=allow protocol=TCP localport=38393
netsh advfirewall firewall add rule name="ClickBridge mDNS" dir=in action=allow protocol=UDP localport=5353
echo.
echo Firewall rules added (TCP 38393 inbound, UDP 5353 inbound).
pause
