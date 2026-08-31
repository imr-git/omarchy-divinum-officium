import QtQuick
import QtQuick.Shapes
import qs.Commons

// A font-independent cross pattée. Drawing the mark as a filled vector keeps
// its optical size consistent across themes while still inheriting the bar's
// foreground color.
Item {
  id: root

  property real iconSize: Style.bar.iconCanvas
  property color color: Color.foreground

  width: iconSize
  height: iconSize
  implicitWidth: iconSize
  implicitHeight: iconSize

  Shape {
    anchors.fill: parent
    antialiasing: true
    preferredRendererType: Shape.CurveRenderer

    ShapePath {
      fillColor: root.color
      strokeColor: "transparent"
      strokeWidth: 0
      startX: root.width * 0.36
      startY: root.height * 0.36

      PathCubic {
        control1X: root.width * 0.34; control1Y: root.height * 0.28
        control2X: root.width * 0.29; control2Y: root.height * 0.16
        x: root.width * 0.22; y: root.height * 0.06
      }
      PathLine { x: root.width * 0.78; y: root.height * 0.06 }
      PathCubic {
        control1X: root.width * 0.71; control1Y: root.height * 0.16
        control2X: root.width * 0.66; control2Y: root.height * 0.28
        x: root.width * 0.64; y: root.height * 0.36
      }
      PathCubic {
        control1X: root.width * 0.72; control1Y: root.height * 0.34
        control2X: root.width * 0.84; control2Y: root.height * 0.29
        x: root.width * 0.94; y: root.height * 0.22
      }
      PathLine { x: root.width * 0.94; y: root.height * 0.78 }
      PathCubic {
        control1X: root.width * 0.84; control1Y: root.height * 0.71
        control2X: root.width * 0.72; control2Y: root.height * 0.66
        x: root.width * 0.64; y: root.height * 0.64
      }
      PathCubic {
        control1X: root.width * 0.66; control1Y: root.height * 0.72
        control2X: root.width * 0.71; control2Y: root.height * 0.84
        x: root.width * 0.78; y: root.height * 0.94
      }
      PathLine { x: root.width * 0.22; y: root.height * 0.94 }
      PathCubic {
        control1X: root.width * 0.29; control1Y: root.height * 0.84
        control2X: root.width * 0.34; control2Y: root.height * 0.72
        x: root.width * 0.36; y: root.height * 0.64
      }
      PathCubic {
        control1X: root.width * 0.28; control1Y: root.height * 0.66
        control2X: root.width * 0.16; control2Y: root.height * 0.71
        x: root.width * 0.06; y: root.height * 0.78
      }
      PathLine { x: root.width * 0.06; y: root.height * 0.22 }
      PathCubic {
        control1X: root.width * 0.16; control1Y: root.height * 0.29
        control2X: root.width * 0.28; control2Y: root.height * 0.34
        x: root.width * 0.36; y: root.height * 0.36
      }
    }
  }
}
