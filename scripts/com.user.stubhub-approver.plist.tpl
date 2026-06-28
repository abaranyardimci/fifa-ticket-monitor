<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.stubhub-approver</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>__REPO_DIR__/scripts/stubhub_approver_run.sh</string>
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

    <!-- Always-on local server so the email "Approve" links work whenever the
         Mac is awake. Restarts if it crashes. -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/stubhub-approver.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/Library/Logs/stubhub-approver.launchd.err.log</string>
</dict>
</plist>
