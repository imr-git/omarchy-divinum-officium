import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.imr-git.divinum-officium"

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  // Keep Omarchy's open-panel mark on the exact pixels painted by our
  // custom content instead of deriving it from the padded button width.
  readonly property real openPanelIndicatorWidth: contentRow.width
  readonly property real openPanelIndicatorHeight: Math.max(Style.space(10), Math.round(Style.bar.iconSlot * 0.55))

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function refresh(force) { if (panelLoader.item) panelLoader.item.refresh(force === true) }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    fixedWidth: contentRow.implicitWidth + scaledHorizontalMargin * 2
    tooltipText: panelLoader.item ? panelLoader.item.tooltipText : "Divine Office"

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) root.refresh(true)
      else root.toggle()
    }

    Row {
      id: contentRow
      anchors.verticalCenter: parent.verticalCenter
      x: Math.round((parent.width - width) / 2)
      width: Math.round(implicitWidth)
      spacing: Style.space(7)

      TatzenkreuzIcon {
        anchors.verticalCenter: parent.verticalCenter
        iconSize: 11
        color: button.foreground
      }

      Text {
        visible: panelLoader.item ? panelLoader.item.showHourName : true
        anchors.verticalCenter: parent.verticalCenter
        text: panelLoader.item ? panelLoader.item.barLabel : "Office"
        textFormat: Text.PlainText
        color: button.foreground
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }
    }
  }
}
