<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.pbcourt-monitor</string>

    <!-- Invoke /bin/bash so we get a real shell environment (PATH, etc.). -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>__REPO_DIR__/scripts/pbcourt_run.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>__REPO_DIR__</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PBCOURT_REPO_DIR</key>
        <string>__REPO_DIR__</string>
    </dict>

    <!-- Fire every hour at minute :47 (offsets from FIFA/ATC cron slots if
         those ever come back). launchd skips firings while the Mac is asleep;
         it does not run a "make-up" job on wake. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Minute</key>
        <integer>47</integer>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>ProcessType</key>
    <string>Background</string>

    <!-- launchd-level logs (the wrapper also writes to ~/Library/Logs/pbcourt-monitor.log). -->
    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/pbcourt-monitor.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/Library/Logs/pbcourt-monitor.launchd.err.log</string>
</dict>
</plist>
