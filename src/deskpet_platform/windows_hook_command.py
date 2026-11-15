"""Experimental fail-open Windows hook proxy; not installed in user settings yet."""
import base64
import subprocess


_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::InputEncoding = [Text.UTF8Encoding]::new($false); [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch {}
function Decode([string]$value) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value)) }
$result = '{}'
$process = $null
try {
    $data = [IO.MemoryStream]::new()
    [Console]::OpenStandardInput().CopyTo($data)
    $bytes = $data.ToArray(); $data.Dispose()
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Decode '__EXE__'
    $info.Arguments = Decode '__ARGS__'
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true; $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
    if ($info.PSObject.Properties['StandardInputEncoding']) { $info.StandardInputEncoding = [Text.UTF8Encoding]::new($false) }
    $info.EnvironmentVariables['PYTHONUTF8'] = '1'
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $info
    if (!$process.Start()) { throw 'start failed' }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $output = $process.StandardOutput.ReadToEndAsync()
    $errors = $process.StandardError.BaseStream.CopyToAsync([IO.Stream]::Null)
    $write = $process.StandardInput.BaseStream.WriteAsync($bytes, 0, $bytes.Length)
    if (!$write.Wait(1500)) { throw 'input timeout' }
    $null = $write.GetAwaiter().GetResult(); $process.StandardInput.Close()
    $remaining = 1500 - [int]$watch.ElapsedMilliseconds
    if ($remaining -le 0 -or !$process.WaitForExit($remaining)) { throw 'hook timeout' }
    if ($process.ExitCode -eq 0) {
        $value = $output.GetAwaiter().GetResult()
        if ($value.Trim().Length -gt 0) { $result = $value }
    }
} catch {
    try { if ($null -ne $process -and !$process.HasExited) { $process.Kill(); $null = $process.WaitForExit(500) } } catch {}
} finally { if ($null -ne $process) { $process.Dispose() } }
[Console]::Out.Write($result)
exit 0
'''.lstrip()


def build(executable, arguments):
    def encode(value):
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    script = _SCRIPT.replace("__EXE__", encode(str(executable))).replace(
        "__ARGS__", encode(subprocess.list2cmdline(arguments)))
    command = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(
        script.encode("utf-16le")).decode("ascii")
    if len(command) > 7900:
        raise ValueError("提醒命令过长，未修改 Agent 设置。")
    return command
