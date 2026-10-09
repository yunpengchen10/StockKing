Unicode true
!ifndef ARG_STOCKKING_COMPRESSOR
    !define ARG_STOCKKING_COMPRESSOR "lzma"
!endif
!if "${ARG_STOCKKING_COMPRESSOR}" == "lzma"
    SetCompressor /SOLID lzma
!else if "${ARG_STOCKKING_COMPRESSOR}" == "zlib"
    SetCompressor zlib
!else
    !error "Unsupported Stock King compressor"
!endif

!define INFO_PROJECTNAME "stock-king"
!define INFO_COMPANYNAME "Stock King"
!define INFO_PRODUCTNAME "Stock King"
!ifndef INFO_PRODUCTVERSION
    !define INFO_PRODUCTVERSION "2.6.3"
!endif
!define PRODUCT_EXECUTABLE "Stock King.exe"
!define REQUEST_EXECUTION_LEVEL "user"

# PyTorch ships deeply nested third-party license paths.  Keep a relative
# default for ordinary builds, while allowing the release script to map the
# sidecar parent to a short temporary drive so makensis stays below its source
# path limit on Windows.
!ifndef ARG_STOCKKING_SIDECAR_ROOT
    !define ARG_STOCKKING_SIDECAR_ROOT "..\..\..\..\daily-engine\dist\backend"
!endif

!ifndef ARG_STOCKKING_WEBVIEW2_INSTALLER
    !define ARG_STOCKKING_WEBVIEW2_INSTALLER "tmp\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
!endif
!ifndef ARG_STOCKKING_OUTPUT
    !define ARG_STOCKKING_OUTPUT "..\..\bin\Stock-King-Setup-x64-v${INFO_PRODUCTVERSION}.exe"
!endif

####
## Please note: Template replacements don't work in this file. They are provided with default defines like
## mentioned underneath.
## If the keyword is not defined, "wails_tools.nsh" will populate them with the values from ProjectInfo.
## If they are defined here, "wails_tools.nsh" will not touch them. This allows to use this project.nsi manually
## from outside of Wails for debugging and development of the installer.
##
## For development first make a wails nsis build to populate the "wails_tools.nsh":
## > wails build --target windows/amd64 --nsis
## Then you can call makensis on this file with specifying the path to your binary:
## For a AMD64 only installer:
## > makensis -DARG_WAILS_AMD64_BINARY=..\..\bin\app.exe
## For a ARM64 only installer:
## > makensis -DARG_WAILS_ARM64_BINARY=..\..\bin\app.exe
## For a installer with both architectures:
## > makensis -DARG_WAILS_AMD64_BINARY=..\..\bin\app-amd64.exe -DARG_WAILS_ARM64_BINARY=..\..\bin\app-arm64.exe
####
## The following information is taken from the ProjectInfo file, but they can be overwritten here.
####
## !define INFO_PROJECTNAME    "MyProject" # Default "{{.Name}}"
## !define INFO_COMPANYNAME    "MyCompany" # Default "{{.Info.CompanyName}}"
## !define INFO_PRODUCTNAME    "MyProduct" # Default "{{.Info.ProductName}}"
## !define INFO_PRODUCTVERSION "1.0.0"     # Default "{{.Info.ProductVersion}}"
## !define INFO_COPYRIGHT      "Copyright" # Default "{{.Info.Copyright}}"
###
## !define PRODUCT_EXECUTABLE  "Application.exe"      # Default "${INFO_PROJECTNAME}.exe"
## !define UNINST_KEY_NAME     "UninstKeyInRegistry"  # Default "${INFO_COMPANYNAME}${INFO_PRODUCTNAME}"
####
## !define REQUEST_EXECUTION_LEVEL "admin"            # Default "admin"  see also https://nsis.sourceforge.io/Docs/Chapter4.html
####
## Include the wails tools
####
!include "wails_tools.nsh"

# The version information for this two must consist of 4 parts
VIProductVersion "${INFO_PRODUCTVERSION}.0"
VIFileVersion    "${INFO_PRODUCTVERSION}.0"

VIAddVersionKey "CompanyName"     "${INFO_COMPANYNAME}"
VIAddVersionKey "FileDescription" "${INFO_PRODUCTNAME} Installer"
VIAddVersionKey "ProductVersion"  "${INFO_PRODUCTVERSION}"
VIAddVersionKey "FileVersion"     "${INFO_PRODUCTVERSION}"
VIAddVersionKey "LegalCopyright"  "${INFO_COPYRIGHT}"
VIAddVersionKey "ProductName"     "${INFO_PRODUCTNAME}"

# Enable HiDPI support. https://nsis.sourceforge.io/Reference/ManifestDPIAware
ManifestDPIAware true

!include "MUI.nsh"

!define MUI_ICON "..\icon.ico"
!define MUI_UNICON "..\icon.ico"
!define MUI_FINISHPAGE_NOAUTOCLOSE # Wait on the INSTFILES page so the user can take a look into the details of the installation steps
!define MUI_ABORTWARNING # This will warn the user if they exit from the installer.
!define MUI_FINISHPAGE_RUN "$INSTDIR\${PRODUCT_EXECUTABLE}"
!define MUI_FINISHPAGE_RUN_TEXT "Open Stock King"

!insertmacro MUI_PAGE_WELCOME # Welcome to the installer page.
!insertmacro MUI_PAGE_LICENSE "..\..\..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY # In which folder install page.
!insertmacro MUI_PAGE_INSTFILES # Installing page.
!insertmacro MUI_PAGE_FINISH # Finished installation page.

!insertmacro MUI_UNPAGE_INSTFILES # Uinstalling page

!insertmacro MUI_LANGUAGE "English" # Set the Language of the installer
!insertmacro MUI_LANGUAGE "SimpChinese"

## The following two statements can be used to sign the installer and the uninstaller. The path to the binaries are provided in %1
#!uninstfinalize 'signtool --file "%1"'
#!finalize 'signtool --file "%1"'

Name "${INFO_PRODUCTNAME}"
OutFile "${ARG_STOCKKING_OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\${INFO_PRODUCTNAME}"
InstallDirRegKey HKCU "${UNINST_KEY}" "InstallLocation"
ShowInstDetails show # This will always show the installation details.

Var ExtractOnly

Function .onInit
   !insertmacro wails.checkArchitecture
   StrCpy $ExtractOnly "0"
   ${GetParameters} $R0
   ClearErrors
   ${GetOptions} $R0 "/EXTRACTONLY" $R1
   ${IfNot} ${Errors}
       StrCpy $ExtractOnly "1"
   ${EndIf}
FunctionEnd

!macro stockking.webview2runtime
    SetRegView 64
    ReadRegStr $0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
    ${If} $0 == ""
        ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
    ${EndIf}
    ${If} $0 == ""
        DetailPrint "Installing offline Microsoft WebView2 Runtime"
        InitPluginsDir
        CreateDirectory "$pluginsdir\stockking-webview2"
        SetOutPath "$pluginsdir\stockking-webview2"
        File "${ARG_STOCKKING_WEBVIEW2_INSTALLER}"
        ExecWait '"$pluginsdir\stockking-webview2\MicrosoftEdgeWebView2RuntimeInstallerX64.exe" /silent /install' $0
        ${If} $0 != 0
            Abort "Microsoft WebView2 Runtime installation failed with exit code $0"
        ${EndIf}
    ${EndIf}
!macroend

Section
    !insertmacro wails.setShellContext

    ${If} $ExtractOnly != "1"
        !insertmacro stockking.webview2runtime
    ${EndIf}

    SetOutPath $INSTDIR

    !insertmacro wails.files

    # Anchor the recursive search inside this exact build. Searching for the
    # directory name itself also picks up sibling backend\stock_analysis builds.
    SetOutPath "$INSTDIR\resources\daily-engine\stock_analysis"
    File /r "${ARG_STOCKKING_SIDECAR_ROOT}\stock_analysis\*"

    SetOutPath "$INSTDIR\licenses"
    File "/oname=Stock-King-GPL-3.0.txt" "..\..\..\LICENSE"
    File "/oname=Daily-Stock-Analysis-MIT.txt" "..\..\..\..\daily-engine\LICENSE"
    File "/oname=MASTER-MIT.txt" "..\..\..\..\licenses\MASTER-MIT.txt"
    File "/oname=THIRD-PARTY-NOTICES.md" "..\..\..\..\THIRD_PARTY_NOTICES.md"
    File "/oname=THIRD-PARTY-NOTICES.zh-CN.md" "..\..\..\..\THIRD_PARTY_NOTICES.zh-CN.md"

    SetOutPath "$INSTDIR\resources"
    File "/oname=register-stock-king-tasks.ps1" "..\..\..\..\scripts\register-stock-king-tasks.ps1"

    SetOutPath "$INSTDIR\docs"
    File "/oname=README.md" "..\..\..\..\README.md"
    File "/oname=README.en.md" "..\..\..\..\README.en.md"
    File "/oname=WINDOWS_INSTALL.md" "..\..\..\..\docs\WINDOWS_INSTALL.md"
    File "/oname=WINDOWS_INSTALL.en.md" "..\..\..\..\docs\WINDOWS_INSTALL.en.md"

    SetOutPath $INSTDIR

    # Extraction is used to verify the payload without changing the user's
    # shortcuts, tasks, uninstall registry, WebView2, or application data.
    ${If} $ExtractOnly == "1"
        Goto installed
    ${EndIf}

    ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "$INSTDIR\resources\register-stock-king-tasks.ps1" -InstallDir "$INSTDIR"' $0
    ${If} $0 != 0
        DetailPrint "Scheduled task registration returned exit code $0; the app remains usable and tasks can be repaired later."
    ${EndIf}

    CreateShortcut "$SMPROGRAMS\${INFO_PRODUCTNAME}.lnk" "$INSTDIR\${PRODUCT_EXECUTABLE}"
    CreateShortCut "$DESKTOP\${INFO_PRODUCTNAME}.lnk" "$INSTDIR\${PRODUCT_EXECUTABLE}"

    !insertmacro wails.associateFiles
    !insertmacro wails.associateCustomProtocols

    # RequestExecutionLevel is user: register in HKCU, never rely on HKLM.
    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${INFO_COMPANYNAME}"
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${INFO_PRODUCTNAME}"
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${INFO_PRODUCTVERSION}"
    WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${PRODUCT_EXECUTABLE}"
    WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '$\"$INSTDIR\uninstall.exe$\"'
    WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '$\"$INSTDIR\uninstall.exe$\" /S'
    installed:
SectionEnd

Section "uninstall"
    !insertmacro wails.setShellContext

    Delete "$SMPROGRAMS\${INFO_PRODUCTNAME}.lnk"
    Delete "$DESKTOP\${INFO_PRODUCTNAME}.lnk"

    ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "$INSTDIR\resources\register-stock-king-tasks.ps1" -Uninstall' $0

    !insertmacro wails.unassociateFiles
    !insertmacro wails.unassociateCustomProtocols

    Delete "$INSTDIR\uninstall.exe"
    DeleteRegKey HKCU "${UNINST_KEY}"

    # User databases, settings, models and logs are deliberately stored under
    # %APPDATA%\Stock King and %LOCALAPPDATA%\Stock King and are not removed.
    RMDir /r "$INSTDIR"
SectionEnd
