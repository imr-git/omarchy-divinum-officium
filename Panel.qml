import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "io.github.imr-git.divinum-officium"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property date now: new Date()
  property var report: null
  property var solarReport: null
  property string reportError: ""
  property bool loading: false
  property bool settingsOpen: false
  property bool draftShowHourName: true
  property bool draftSolarSchedule: false
  property string settingsError: ""

  readonly property var versionOptions: [
    "Tridentine - 1570",
    "Tridentine - 1888",
    "Tridentine - 1906",
    "Divino Afflatu - 1939",
    "Divino Afflatu - 1954",
    "Reduced - 1955",
    "Rubrics 1960 - 1960",
    "Rubrics 1960 - 2020 USA"
  ]
  readonly property var languageOptions: [
    "Latin", "English", "Deutsch", "French", "Italiano", "Magyar",
    "Polski", "Portuguese", "Espanol", "Cesky", "Nederlands"
  ]
  readonly property var scheduleFields: [
    { key: "matinsTime", label: "Matins", fallback: "00:00" },
    { key: "laudsTime", label: "Lauds", fallback: "06:00" },
    { key: "primeTime", label: "Prime", fallback: "07:00" },
    { key: "terceTime", label: "Terce", fallback: "09:00" },
    { key: "sextTime", label: "Sext", fallback: "12:00" },
    { key: "noneTime", label: "None", fallback: "15:00" },
    { key: "vespersTime", label: "Vespers", fallback: "18:00" },
    { key: "complineTime", label: "Compline", fallback: "21:00" }
  ]

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color accent: bar ? bar.urgent : Color.accent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool solarScheduleEnabled: String(setting("scheduleMode", "fixed")) === "solar"
  readonly property bool solarScheduleAvailable: solarReport !== null
    && solarReport.error === ""
    && solarReport.schedule !== undefined
  readonly property var hours: Model.schedule(settings, solarScheduleEnabled ? solarReport : null)
  readonly property int currentIndex: Model.currentHourIndex(now, hours)
  readonly property int nextIndex: Model.nextHourIndex(currentIndex, hours)
  readonly property var currentHour: currentIndex >= 0 ? hours[currentIndex] : null
  readonly property var nextHour: nextIndex >= 0 ? hours[nextIndex] : null
  readonly property string version: String(setting("version", "Tridentine - 1570"))
  readonly property string primaryLanguage: String(setting("primaryLanguage", "Latin"))
  readonly property string secondaryLanguage: String(setting("secondaryLanguage", "English"))
  readonly property bool showHourName: setting("showCurrentHourInBar", true) === true
  readonly property string barLabel: currentHour ? currentHour.name : "Office"
  readonly property string tooltipText: nextHour
    ? currentHour.name + " · " + nextHour.name + " " + Model.remainingLabel(Model.minutesUntilNext(now, currentIndex, hours))
    : "Divine Office"
  readonly property string helperPath: String(Qt.resolvedUrl("omadivoff.py")).replace(/^file:\/\//, "")

  function setting(key, fallback) {
    return settings && settings[key] !== undefined ? settings[key] : fallback
  }

  function liturgicalColor(value) {
    switch (String(value || "").toLowerCase()) {
    case "red": return "#c75b62"
    case "green": return "#6f9f68"
    case "violet": return "#8b6bae"
    case "rose": return "#cc82a6"
    case "black": return "#353535"
    case "white": return "#eeeae0"
    default: return "transparent"
    }
  }

  function open() {
    root.controller.show()
    root.refresh()
    root.refreshSolar()
  }

  function close() {
    root.settingsOpen = false
    root.settingsError = ""
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  function persistSettings(values) {
    var entry = { id: root.moduleName }
    var current = root.settings || {}
    for (var existing in current)
      if (existing !== "id") entry[existing] = current[existing]
    for (var key in values) entry[key] = values[key]

    root.settings = entry
    if (root.hostWidget && "settings" in root.hostWidget)
      root.hostWidget.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function openSettings() {
    root.settingsError = ""
    root.draftShowHourName = root.showHourName
    root.draftSolarSchedule = root.solarScheduleEnabled
    versionField.value = root.version
    primaryLanguageField.value = root.primaryLanguage
    secondaryLanguageField.value = root.secondaryLanguage
    for (var i = 0; i < root.scheduleFields.length; i++) {
      var item = scheduleRepeater.itemAt(i)
      if (item)
        item.value = String(root.setting(root.scheduleFields[i].key, root.scheduleFields[i].fallback))
    }
    root.settingsOpen = true
    root.refreshSolar()
  }

  function cancelSettings() {
    root.settingsOpen = false
    root.settingsError = ""
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function saveSettings() {
    var values = {
      version: versionField.value,
      primaryLanguage: primaryLanguageField.value,
      secondaryLanguage: secondaryLanguageField.value,
      showCurrentHourInBar: root.draftShowHourName,
      scheduleMode: root.draftSolarSchedule ? "solar" : "fixed"
    }

    if (root.draftSolarSchedule && !root.solarScheduleAvailable) {
      root.settingsError = root.solarReport && root.solarReport.error
        ? root.solarReport.error
        : "The local solar schedule is still loading."
      return
    }

    for (var i = 0; i < root.scheduleFields.length; i++) {
      var field = root.scheduleFields[i]
      var item = scheduleRepeater.itemAt(i)
      var value = item ? String(item.value).trim() : field.fallback
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) {
        root.settingsError = field.label + " must use 24-hour HH:MM format."
        return
      }
      values[field.key] = value
    }

    root.persistSettings(values)
    root.settingsOpen = false
    root.settingsError = ""
    root.now = new Date()
    root.refresh()
    root.refreshSolar()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function refresh() {
    if (reportProc.running) return
    root.loading = true
    root.reportError = ""
    reportProc.command = [
      "python3", root.helperPath, "report",
      "--date", Model.isoDate(root.now),
      "--version", root.version,
      "--primary-language", root.primaryLanguage,
      "--secondary-language", root.secondaryLanguage
    ]
    reportProc.running = true
  }

  function refreshSolar() {
    if (solarProc.running) return
    solarProc.command = [
      "python3", root.helperPath, "solar",
      "--date", Model.isoDate(root.now)
    ]
    solarProc.running = true
  }

  function openExternal(url) {
    if (!url) return
    root.close()
    Quickshell.execDetached(["omarchy-launch-browser", url])
  }

  function openOffice(hour) {
    if (!hour) return
    root.openExternal(Model.officeUrl(root.now, hour.command, root.version, root.primaryLanguage, root.secondaryLanguage))
  }

  function openMass() {
    root.openExternal(Model.massUrl(root.now, root.version, root.primaryLanguage, root.secondaryLanguage))
  }

  function openSaintInfo() {
    if (!root.report || !root.report.title) return
    root.openExternal(Model.martyrologyUrl(root.now))
  }

  Process {
    id: reportProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = Model.parseReport(text)
        if (parsed) {
          root.report = parsed
          root.reportError = parsed.error || ""
        } else {
          root.reportError = "Could not read the daily liturgical metadata."
        }
      }
    }
    onExited: function(exitCode) {
      root.loading = false
      if (exitCode !== 0 && root.reportError === "")
        root.reportError = "Divinum Officium is temporarily unavailable."
    }
  }

  Process {
    id: solarProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = Model.parseReport(text)
        root.solarReport = parsed || {
          error: "Could not calculate the local solar schedule."
        }
      }
    }
    onExited: function(exitCode) {
      if (exitCode !== 0 && (!root.solarReport || !root.solarReport.error))
        root.solarReport = { error: "Could not calculate the local solar schedule." }
    }
  }

  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: {
      var previousDate = Model.isoDate(root.now)
      root.now = new Date()
      if (previousDate !== Model.isoDate(root.now)) {
        root.refresh()
        root.refreshSolar()
      }
    }
  }

  Timer {
    interval: 6 * 60 * 60 * 1000
    running: true
    repeat: true
    onTriggered: {
      root.refresh()
      root.refreshSolar()
    }
  }

  Component.onCompleted: {
    refresh()
    refreshSolar()
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (!root.settingsOpen && (text === "r" || text === "R")) root.refresh()
      }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true

        Column {
          id: content
          width: parent.width
          spacing: Style.space(10)

          Column {
            id: dashboard
            visible: !root.settingsOpen
            width: parent.width
            spacing: Style.space(10)

          Text {
            id: feastTitle
            width: parent.width
            text: root.report && root.report.title ? root.report.title : Qt.formatDate(root.now, "dddd, d MMMM yyyy")
            textFormat: Text.PlainText
            color: feastTitleMouse.containsMouse ? root.accent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            font.underline: feastTitleMouse.containsMouse
            wrapMode: Text.WordWrap

            MouseArea {
              id: feastTitleMouse
              enabled: root.report && root.report.title
              anchors.left: parent.left
              anchors.top: parent.top
              width: Math.min(parent.width, parent.contentWidth)
              height: parent.contentHeight
              hoverEnabled: true
              cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
              onClicked: root.openSaintInfo()
            }
          }

          Row {
            visible: metadataText.text !== ""
            width: parent.width
            spacing: Style.space(6)

            Rectangle {
              id: liturgicalColorMarker
              visible: root.report && root.report.color
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(7)
              height: width
              radius: width / 2
              color: root.liturgicalColor(root.report ? root.report.color : "")
              border.width: Style.spacing.hairline
              border.color: root.dim
            }

            Text {
              id: metadataText
              width: Math.max(0, parent.width - (liturgicalColorMarker.visible ? liturgicalColorMarker.width + parent.spacing : 0))
              text: root.report
                ? [root.report.rank || "", root.report.season || ""].filter(function(value) { return value !== "" }).join(" · ")
                : ""
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Text {
            visible: root.report && root.report.commemorations && root.report.commemorations.length > 0
            width: parent.width
            text: root.report && root.report.commemorations ? "Commemorations: " + root.report.commemorations.join("; ") : ""
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Rectangle {
            width: parent.width
            height: Style.spacing.controlHeight
            radius: Style.cornerRadius
            color: massMouse.containsMouse
              ? Style.hoverFillFor(root.foreground, Color.accent)
              : Style.selectedFillFor(root.foreground, Color.accent)

            Row {
              anchors.fill: parent
              anchors.leftMargin: Style.spacing.controlPaddingX
              anchors.rightMargin: Style.spacing.controlPaddingX
              spacing: Style.spacing.md

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Mass of the Day"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }

              Item { width: Math.max(0, parent.width - massLabel.implicitWidth - massAction.implicitWidth - Style.space(26)); height: 1 }

              Text {
                id: massLabel
                visible: false
                text: "Mass of the Day"
              }

              Text {
                id: massAction
                anchors.verticalCenter: parent.verticalCenter
                text: "Open  →"
                color: root.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }
            }

            MouseArea {
              id: massMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.openMass()
            }
          }

          PanelSeparator { foreground: root.foreground }

          Text {
            width: parent.width
            text: "THE HOURS"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 1.2
          }

          Text {
            visible: root.solarScheduleEnabled
            width: parent.width
            text: root.solarScheduleAvailable
              ? "Solar schedule · " + root.solarReport.location.name
                + " · Sunrise " + root.solarReport.sunrise
                + " · Sunset " + root.solarReport.sunset
              : (root.solarReport === null
                ? "Calculating local solar schedule…"
                : "Solar schedule unavailable · using fixed times")
            textFormat: Text.PlainText
            color: root.solarScheduleAvailable ? root.dim : root.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: root.hours

            Rectangle {
              required property var modelData
              required property int index
              readonly property bool isCurrent: index === root.currentIndex
              width: content.width
              height: Math.max(Style.spacing.controlHeight, hourLabels.implicitHeight + Style.spacing.md)
              radius: Style.cornerRadius
              color: isCurrent
                ? Style.selectedFillFor(root.foreground, Color.accent)
                : (hourMouse.containsMouse ? Style.hoverFillFor(root.foreground, Color.accent) : "transparent")

              Rectangle {
                visible: parent.isCurrent
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                width: Style.spacing.hairline
                height: parent.height - Style.spacing.md
                radius: width
                color: root.accent
              }

              Row {
                anchors.fill: parent
                anchors.leftMargin: Style.space(8)
                anchors.rightMargin: Style.space(8)

                Column {
                  id: hourLabels
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(104)
                  spacing: 0

                  Text {
                    text: parent.parent.parent.modelData.name
                    color: parent.parent.parent.isCurrent ? root.accent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: parent.parent.parent.isCurrent
                  }

                  Text {
                    text: parent.parent.parent.modelData.latin
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(48)
                  text: parent.parent.modelData.time
                  color: parent.parent.isCurrent ? root.foreground : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  width: Math.max(0, parent.width - Style.space(152))
                  horizontalAlignment: Text.AlignRight
                  text: parent.parent.isCurrent
                    ? Model.remainingCompactLabel(Model.minutesUntilNext(root.now, root.currentIndex, root.hours)) + "  →"
                    : "Open  →"
                  color: parent.parent.isCurrent || hourMouse.containsMouse ? root.accent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: parent.parent.isCurrent
                }
              }

              MouseArea {
                id: hourMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.openOffice(parent.modelData)
              }
            }
          }

          Text {
            visible: root.loading || root.reportError !== ""
            width: parent.width
            text: root.loading ? "Loading today’s feast…" : root.reportError
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Item {
            width: parent.width
            height: Math.max(versionLabel.implicitHeight, footerSettingsButton.implicitHeight)

            Text {
              id: versionLabel
              anchors.left: parent.left
              anchors.right: footerSettingsButton.left
              anchors.verticalCenter: parent.verticalCenter
              text: root.version + " · " + root.primaryLanguage + " / " + root.secondaryLanguage
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              horizontalAlignment: Text.AlignHCenter
              elide: Text.ElideRight
            }

            PanelActionButton {
              id: footerSettingsButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              width: implicitWidth
              height: implicitHeight
              iconText: "󰒓"
              tooltipText: "Divine Office settings"
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.subtitle
              size: Style.space(26)
              focusable: true
              onClicked: root.openSettings()
            }
          }

          }

          Column {
            id: settingsForm
            visible: root.settingsOpen
            width: parent.width
            spacing: Style.space(10)

            Item {
              width: parent.width
              height: Math.max(settingsTitle.implicitHeight, settingsCloseButton.implicitHeight)

              Text {
                id: settingsTitle
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: "DIVINE OFFICE SETTINGS"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
              }

              PanelActionButton {
                id: settingsCloseButton
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: implicitWidth
                height: implicitHeight
                iconText: "×"
                tooltipText: "Cancel"
                foreground: root.foreground
                fontFamily: root.fontFamily
                focusable: true
                onClicked: root.cancelSettings()
              }
            }

            Text {
              width: parent.width
              text: "Choose the rubrics and languages used when opening Divinum Officium."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Dropdown {
              id: versionField
              width: parent.width
              height: implicitHeight
              label: "Office version"
              value: root.version
              options: root.versionOptions
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
            }

            Dropdown {
              id: primaryLanguageField
              width: parent.width
              height: implicitHeight
              label: "Primary language"
              value: root.primaryLanguage
              options: root.languageOptions
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
            }

            Dropdown {
              id: secondaryLanguageField
              width: parent.width
              height: implicitHeight
              label: "Parallel language"
              value: root.secondaryLanguage
              options: root.languageOptions
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
            }

            Toggle {
              width: parent.width
              height: implicitHeight
              label: "Show hour name in the bar"
              description: "Turn this off to show only the cross mark."
              checked: root.draftShowHourName
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              onClicked: root.draftShowHourName = !root.draftShowHourName
            }

            Toggle {
              width: parent.width
              height: implicitHeight
              label: "Follow sunrise and sunset"
              description: "Uses the location configured in Omarchy Weather. Fixed times remain saved as the fallback."
              checked: root.draftSolarSchedule
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              onClicked: {
                root.draftSolarSchedule = !root.draftSolarSchedule
                if (root.draftSolarSchedule) root.refreshSolar()
              }
            }

            PanelSeparator { foreground: root.foreground }

            Text {
              width: parent.width
              text: root.draftSolarSchedule ? "SOLAR SCHEDULE" : "HOUR BOUNDARIES"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
            }

            Text {
              width: parent.width
              text: root.draftSolarSchedule
                ? "Matins follows the eighth hour of night; Lauds civil dawn; Prime sunrise; Terce, Sext, and None divide daylight; Vespers sunset; Compline one hour later."
                : "Set the local time when each hour becomes current, using 24-hour HH:MM."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Text {
              visible: root.draftSolarSchedule
              width: parent.width
              text: root.solarScheduleAvailable
                ? root.solarReport.location.name
                  + " · Sunrise " + root.solarReport.sunrise
                  + " · Sunset " + root.solarReport.sunset
                : (root.solarReport && root.solarReport.error
                  ? root.solarReport.error
                  : "Calculating today’s solar schedule…")
              color: root.solarScheduleAvailable ? root.foreground : root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Repeater {
              id: scheduleRepeater
              model: root.scheduleFields

              Item {
                required property var modelData
                property alias value: timeField.text
                width: settingsForm.width
                height: Math.max(
                  scheduleLabel.implicitHeight,
                  root.draftSolarSchedule ? solarTime.implicitHeight : timeField.implicitHeight
                )

                Text {
                  id: scheduleLabel
                  anchors.left: parent.left
                  anchors.verticalCenter: parent.verticalCenter
                  text: parent.modelData.label
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }

                TextField {
                  id: timeField
                  visible: !root.draftSolarSchedule
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(88)
                  height: implicitHeight
                  text: String(root.setting(parent.modelData.key, parent.modelData.fallback))
                  placeholderText: parent.modelData.fallback
                  horizontalAlignment: TextInput.AlignHCenter
                  foreground: root.foreground
                  accent: root.accent
                  font.family: root.fontFamily
                  onAccepted: root.saveSettings()
                  Keys.onPressed: function(event) {
                    if (event.key === Qt.Key_Escape) {
                      root.cancelSettings()
                      event.accepted = true
                    }
                  }
                }

                Text {
                  id: solarTime
                  visible: root.draftSolarSchedule
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.solarScheduleAvailable
                    ? String(root.solarReport.schedule[parent.modelData.key] || "—")
                    : "—"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
              }
            }

            Text {
              visible: root.settingsError !== ""
              width: parent.width
              text: root.settingsError
              color: root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Row {
              width: parent.width
              spacing: Style.spacing.md

              Button {
                width: (parent.width - parent.spacing) / 2
                height: implicitHeight
                text: "Cancel"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                bordered: true
                focusable: true
                onClicked: root.cancelSettings()
              }

              Button {
                width: (parent.width - parent.spacing) / 2
                height: implicitHeight
                text: "Save"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                selected: true
                bordered: true
                focusable: true
                onClicked: root.saveSettings()
              }
            }
          }
        }
      }
    }
  }
}
