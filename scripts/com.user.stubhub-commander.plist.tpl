<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.stubhub-commander</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>__REPO_DIR__/scripts/stubhub_commander_run.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>__REPO_DIR__</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>STUBHUB_REPO_DIR</key>
        <string>__REPO_DIR__</string>
    </dict>

    <!-- Always-on poller so email Approve/Decline/Modify replies are acted on
         within ~1 poll interval whenever the Mac is awake. Restarts if it
         crashes. Outbound IMAP only; no inbound port. -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/stubhub-commander.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/Library/Logs/stubhub-commander.launchd.err.log</string>
</dict>
</plist>
