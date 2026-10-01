' Token Saver 后台常驻启动器（无窗口）
' 双击即可以无控制台方式在后台启动服务，再用浏览器打开控制台。
' 注意：控制台里的「叫停全部」只停任务、不停进程；要停进程请用任务管理器
'       结束 python.exe，或改跑「启动服务.bat」按 Ctrl+C。
Option Explicit

Dim sh, fso, here, homeDir, py
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 以脚本所在目录为技能目录 —— 不写死任何绝对路径，换机器/换目录都能用
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here

' 虚拟环境在技能目录之外（见 scripts/install.py 的说明）
homeDir = sh.ExpandEnvironmentStrings("%TOKEN_SAVER_HOME%")
If homeDir = "%TOKEN_SAVER_HOME%" Then
    homeDir = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.token-saver"
End If

py = homeDir & "\venv\Scripts\python.exe"
If Not fso.FileExists(py) Then
    MsgBox "找不到虚拟环境：" & vbCrLf & py & vbCrLf & vbCrLf & _
           "请先在技能目录里运行：python scripts\install.py", _
           16, "Token Saver"
    WScript.Quit 1
End If

' `-B` = 不写 .pyc：这个目录是要打包分发的，缓存属于运行期产物
' （和 data/、venv/ 一样该落在代码目录之外）。
' 用命令行开关而不是改环境变量 —— 开关的行为一眼可见，也更好验证。
sh.Run """" & py & """ -B """ & here & "\server.py"" --no-open", 0, False
Set sh = Nothing
Set fso = Nothing
