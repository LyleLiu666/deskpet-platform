"""Build a reversible Windows status-line wrapper without storing its input on disk."""
import base64
import gzip
import io
import json
import subprocess

PREFIX = "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
MARKER = "# deskpet-quota-windows-v2-"
BOOT_PREFIX = "$ProgressPreference='SilentlyContinue';$null='deskpet-quota-windows-v2';$z=[Convert]::FromBase64String('"
BOOT_SUFFIX = "');$s=[IO.Compression.GzipStream]::new([IO.MemoryStream]::new($z),[IO.Compression.CompressionMode]::Decompress);iex (([IO.StreamReader]::new($s)).ReadToEnd())"

# This script is embedded in the Agent's setting. It can still forward input
# to the original command if the DeskPet Python environment is removed.
SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::InputEncoding = [Text.UTF8Encoding]::new($false) } catch {}
function Decode([string]$value) {
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value))
}
$meta = (Decode '__METADATA__') | ConvertFrom-Json
$captureArgs = Decode '__CAPTURE_ARGS__'
$previousArgs = Decode '__PREVIOUS_ARGS__'
$inputStream = [IO.MemoryStream]::new()
[Console]::OpenStandardInput().CopyTo($inputStream)
$data = $inputStream.ToArray()
$inputStream.Dispose()
function RunCommand([string]$exe, [string]$arguments, [byte[]]$bytes, [bool]$discard) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $exe
    $info.Arguments = $arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    if ($info.PSObject.Properties["StandardInputEncoding"]) {
        $info.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
    }
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        if (!$process.Start()) { return -1 }
        $watch = [Diagnostics.Stopwatch]::StartNew()
        if ($discard) { $out = [IO.Stream]::Null; $err = [IO.Stream]::Null }
        else { $out = [Console]::OpenStandardOutput(); $err = [Console]::OpenStandardError() }
        $outTask = $process.StandardOutput.BaseStream.CopyToAsync($out)
        $errTask = $process.StandardError.BaseStream.CopyToAsync($err)
        try {
            $write = $process.StandardInput.BaseStream.WriteAsync($bytes, 0, $bytes.Length)
            if ($discard -and !$write.Wait(1500)) {
                $process.Kill(); $null = $process.WaitForExit(500); return -1
            }
            $null = $write.GetAwaiter().GetResult()
            $process.StandardInput.Close()
        } catch { try { $process.StandardInput.Close() } catch {} }
        if ($discard) {
            $remaining = 1500 - [int]$watch.ElapsedMilliseconds
            if ($remaining -le 0 -or !$process.WaitForExit($remaining)) {
                $process.Kill(); $null = $process.WaitForExit(500); return -1
            }
        }
        $process.WaitForExit()
        $null = $outTask.GetAwaiter().GetResult()
        $null = $errTask.GetAwaiter().GetResult()
        return $process.ExitCode
    } catch { return -1 }
    finally { $process.Dispose() }
}
$ignored = RunCommand $meta.executable $captureArgs $data $true
if ($null -ne $meta.original_command -and $meta.original_command.Length -gt 0) {
    $result = RunCommand ($meta.windows_shell[1]) $previousArgs $data $false
    if ($result -lt 0) { exit 1 }
    exit $result
}
exit 0
'''.lstrip()


def _b64(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _script(metadata):
    shell, executable = metadata["windows_shell"]
    original = metadata["original_command"] or ""
    limit = 7500 if shell == "cmd" else 24000
    if len(original.encode("utf-16le")) // 2 > limit:
        raise ValueError("状态栏命令过长，未修改 Agent 设置。")
    if shell == "bash":
        previous_args = subprocess.list2cmdline(["-c", original])
    elif shell == "powershell":
        previous_args = subprocess.list2cmdline(["-NoProfile", "-NonInteractive", "-Command", original])
    elif shell == "cmd":
        previous_args = f'/d /s /c "{original}"'
    else:
        raise ValueError("不支持的 Windows 状态栏命令环境。")
    capture_args = subprocess.list2cmdline(["-m", "deskpet_platform.quota_capture",
                                            metadata["source"], metadata["store"]])
    encoded_meta = _b64(json.dumps(metadata, ensure_ascii=True, separators=(",", ":")))
    script = (MARKER + encoded_meta + "\n" + SCRIPT.replace("__METADATA__", encoded_meta)
              .replace("__CAPTURE_ARGS__", _b64(capture_args))
              .replace("__PREVIOUS_ARGS__", _b64(previous_args)))
    return script


def command(metadata):
    script = _script(metadata)
    compressed = base64.b64encode(gzip.compress(script.encode("utf-8"), mtime=0)).decode("ascii")
    bootstrap = BOOT_PREFIX + compressed + BOOT_SUFFIX
    result = PREFIX + base64.b64encode(bootstrap.encode("utf-16le")).decode("ascii")
    # cmd.exe limits the complete command line to 8191 characters.
    if len(result) > 7900:
        raise ValueError("状态栏命令过长，未修改 Agent 设置。")
    return result


def metadata(command_text):
    if not isinstance(command_text, str) or not command_text.startswith(PREFIX):
        return None
    try:
        bootstrap = base64.b64decode(command_text[len(PREFIX):], validate=True).decode("utf-16le")
        if not bootstrap.startswith(BOOT_PREFIX):
            return None
        if not bootstrap.endswith(BOOT_SUFFIX):
            raise ValueError
        compressed = base64.b64decode(bootstrap[len(BOOT_PREFIX):-len(BOOT_SUFFIX)], validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            script = stream.read(131073).decode("utf-8")
        if len(script) > 131072:
            raise ValueError
        first = script.split("\n", 1)[0]
        if not first.startswith(MARKER):
            return None
        data = json.loads(base64.b64decode(first[len(MARKER):], validate=True))
        if _script(data) != script:
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError, UnicodeError, IndexError, OSError, EOFError):
        raise ValueError("DeskPet 额度状态栏配置已被修改，请先检查原有设置。") from None
