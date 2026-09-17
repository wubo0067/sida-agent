<#,,,!
.SYNOPSIS
    串行执行全部 PDF build 任务（uv run python main.py --stage build ...）。

.DESCRIPTION
    任务在 $Jobs 中按顺序定义，逐个串行执行；每个任务记录退出码与耗时，
    全部执行完毕后打印汇总表；只要有一个任务失败，脚本以非零码退出。

    默认每执行完一个任务会询问是否继续：直接回车或 Y 继续，N 停止（已完成
    的任务已落盘并缓存，重新运行本脚本即可从剩余任务继续），A 则剩余任务
    不再询问全部自动执行。

.PARAMETER StopOnFailure
    默认遇到失败继续执行后续任务；加此开关后遇到失败立即停止。

.PARAMETER NoConfirm
    跳过每个任务后的「是否继续」询问，一口气跑完（无人值守 / 夜间批量用）。

.EXAMPLE
    .\run_build.ps1
    .\run_build.ps1 -StopOnFailure
    .\run_build.ps1 -NoConfirm
#>
param(
    [switch]$StopOnFailure,
    [switch]$NoConfirm
)

Set-Location -LiteralPath $PSScriptRoot

# ---------- PDF 文件 ----------
$MathPdf = 'L:\vivi\初三\数学26暑9A+暑假汇总版笔记.pdf'
$Physics1 = 'L:\vivi\初三\物理\初中物理重点概念大全.pdf'
$Physics2 = 'P:\初中学习资料\初中各科知识点总结\初中物理知识点归纳总结\初中物理知识点归纳总结.pdf'
$Physics3 = 'L:\vivi\初三\物理\补充：库仑定律知识点与例题.pdf'
$Physics4 = 'L:\vivi\初三\中考物理必备公式.pdf'
$Chem1 = 'L:\vivi\初三\中考常考化学反应方程式.pdf'

# ---------- 任务列表（按顺序串行执行） ----------
$Jobs = @(
    @{ Name = '数学26暑9A+笔记 p42-75'; Pdf = $MathPdf; Start = 42; End = 75; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p77-98'; Pdf = $MathPdf; Start = 77; End = 98; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p100-121'; Pdf = $MathPdf; Start = 100; End = 121; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p123-135'; Pdf = $MathPdf; Start = 123; End = 135; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p138-155'; Pdf = $MathPdf; Start = 138; End = 155; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p157-164'; Pdf = $MathPdf; Start = 157; End = 164; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p166-170'; Pdf = $MathPdf; Start = 166; End = 170; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p172-185'; Pdf = $MathPdf; Start = 172; End = 185; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p187-207'; Pdf = $MathPdf; Start = 187; End = 207; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p209-228'; Pdf = $MathPdf; Start = 209; End = 228; Subject = 'math' },
    @{ Name = '数学26暑9A+笔记 p230-246'; Pdf = $MathPdf; Start = 230; End = 246; Subject = 'math' },
    @{ Name = '初中物理重点概念大全 p1-5'; Pdf = $Physics1; Start = 1; End = 5; Subject = 'physics' },
    @{ Name = '初中物理知识点归纳总结 p1-15'; Pdf = $Physics2; Start = 1; End = 15; Subject = 'physics' },
    @{ Name = '初中物理知识点归纳总结 p17-23'; Pdf = $Physics2; Start = 17; End = 23; Subject = 'physics' },
    @{ Name = '补充：库仑定律知识点与例题 p1-3'; Pdf = $Physics3; Start = 1; End = 3; Subject = 'physics' },
    @{ Name = '中考物理必备公式 p1-2'; Pdf = $Physics4; Start = 1; End = 2; Subject = 'physics' },
    @{ Name = '中考常考化学反应方程式 p1'; Pdf = $Chem1; Start = 1; End = 1; Subject = 'chemistry' }
)

# ---------- 串行执行 ----------
$Results = [System.Collections.Generic.List[object]]::new()
$Index = 0
$AskEach = -not $NoConfirm   # 每个任务跑完后是否询问「是否继续」

foreach ($Job in $Jobs) {
    $Index++
    $Sw = [System.Diagnostics.Stopwatch]::StartNew()

    Write-Host ''
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host "[$Index/$($Jobs.Count)] $($Job.Name)" -ForegroundColor Cyan
    Write-Host "  PDF:   $($Job.Pdf)"
    Write-Host "  Pages: $($Job.Start)-$($Job.End)   Subject: $($Job.Subject)"
    Write-Host "==============================================" -ForegroundColor Cyan

    uv run python main.py --stage build `
        --pdf $Job.Pdf `
        --start-page $Job.Start `
        --end-page $Job.End `
        --subject $Job.Subject `
        --max-new-calls 0 `
        --yes
    $Code = $LASTEXITCODE
    $Sw.Stop()

    if ($Code -eq 0) {
        $Status = 'OK'
        Write-Host "[$Index/$($Jobs.Count)] $($Job.Name)  完成（耗时 $([math]::Round($Sw.TotalSeconds,1))s）" -ForegroundColor Green
    }
    else {
        $Status = "失败(exit=$Code)"
        Write-Host "[$Index/$($Jobs.Count)] $($Job.Name)  失败 exit=$Code（耗时 $([math]::Round($Sw.TotalSeconds,1))s）" -ForegroundColor Red
    }

    $Results.Add([pscustomobject]@{
            Index   = $Index
            任务      = $Job.Name
            页码      = "$($Job.Start)-$($Job.End)"
            Subject = $Job.Subject
            状态      = $Status
            耗时      = $Sw.Elapsed.ToString('mm\:ss')
        })

    if ($Code -ne 0 -and $StopOnFailure) {
        Write-Host '检测到失败且指定了 -StopOnFailure，停止后续任务。' -ForegroundColor Red
        break
    }

    # ---------- 每个任务跑完后询问是否继续 ----------
    if ($AskEach -and $Index -lt $Jobs.Count) {
        $Answer = ''
        try {
            $Answer = (Read-Host '继续执行下一个任务？[Y]继续 / [N]停止 / [A]剩余全部自动继续（默认 Y）').Trim().ToUpper()
        }
        catch {
            Write-Host '无法读取交互输入（非交互环境），按 -NoConfirm 处理，剩余任务自动继续。' -ForegroundColor Yellow
            $AskEach = $false
        }
        if ($Answer -eq 'N') {
            $Rest = @($Jobs[$Index..($Jobs.Count - 1)])
            Write-Host "已手动停止，剩余 $($Rest.Count) 个任务未执行：" -ForegroundColor Yellow
            $Rest | ForEach-Object { Write-Host "  $($_.Name)" -ForegroundColor Yellow }
            Write-Host '已完成的任务已落盘缓存，重新运行本脚本即可继续。' -ForegroundColor Yellow
            break
        }
        elseif ($Answer -eq 'A') {
            $AskEach = $false
        }
    }
}

# ---------- 汇总 ----------
Write-Host ''
Write-Host '============================ 汇总 ============================' -ForegroundColor Cyan
$Results | Format-Table -AutoSize | Out-String | Write-Host

$Failed = @($Results | Where-Object { $_.状态 -ne 'OK' })
if ($Failed.Count -gt 0) {
    Write-Host "共执行 $($Results.Count) 个任务，其中 $($Failed.Count) 个失败：" -ForegroundColor Red
    $Failed | ForEach-Object { Write-Host ("  #{0} {1}（页码 {2}，{3}）" -f $_.Index, $_.任务, $_.页码, $_.状态) -ForegroundColor Red }
    exit 1
}
else {
    Write-Host "全部 $($Results.Count) 个任务执行成功。" -ForegroundColor Green
}
