<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.stubhub-caffeinate</string>

    <!-- Keep the Mac awake so the repricer's scheduled reads fire and the email
         command poller stays responsive. `caffeinate -s` asserts that the SYSTEM
         must not sleep WHILE ON AC POWER (it does NOT assert on battery), and
         needs no sudo. Keep the Mac plugged in for 24/7 monitoring. For
         lid-closed (clamshell) operation also run, once:
             sudo pmset -c disablesleep 1
    -->
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string>
        <string>-s</string>
    </array>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/stubhub-caffeinate.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/Library/Logs/stubhub-caffeinate.launchd.err.log</string>
</dict>
</plist>
