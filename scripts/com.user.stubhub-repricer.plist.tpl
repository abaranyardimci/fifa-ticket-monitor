<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.stubhub-repricer</string>

    <!-- Invoke /bin/bash so we get a real shell environment (PATH, etc.). -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>__REPO_DIR__/scripts/stubhub_repricer_run.sh</string>
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

    <!-- Fire every 2 hours during waking hours at minute :17 (offset from the
         pbcourt monitor's :47). 8 reads/day keeps us well under DataDome's radar
         on a home IP while staying responsive. The scheduled read is ANONYMOUS
         (no login), so it does not risk the StubHub account; the only account-
         touching action is --apply, which runs only when you approve. Tune as
         needed. launchd skips firings while the Mac is asleep; no make-up job. -->
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>17</integer></dict>
        <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>17</integer></dict>
        <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>17</integer></dict>
        <dict><key>Hour</key><integer>14</integer><key>Minute</key><integer>17</integer></dict>
        <dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>17</integer></dict>
        <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>17</integer></dict>
        <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>17</integer></dict>
        <dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>17</integer></dict>
    </array>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/stubhub-repricer.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/Library/Logs/stubhub-repricer.launchd.err.log</string>
</dict>
</plist>
