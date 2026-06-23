# Drawio example

Drop drawio XML inside a fenced block tagged `drawio`. The viewer.diagrams.net library renders it with zoom and lightbox controls.

```drawio
<mxfile host="app.diagrams.net">
  <diagram name="Page-1" id="example">
    <mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="850" pageHeight="1100" math="0" shadow="0">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="2" value="Reader" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#ebe1c5;strokeColor=#8b5a2b;" vertex="1" parent="1">
          <mxGeometry x="80" y="120" width="120" height="60" as="geometry" />
        </mxCell>
        <mxCell id="3" value="Server" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#ebe1c5;strokeColor=#8b5a2b;" vertex="1" parent="1">
          <mxGeometry x="320" y="120" width="120" height="60" as="geometry" />
        </mxCell>
        <mxCell id="4" value="Markdown File" style="shape=note;whiteSpace=wrap;html=1;backgroundOutline=1;fillColor=#f4ecd8;strokeColor=#8b5a2b;" vertex="1" parent="1">
          <mxGeometry x="560" y="120" width="120" height="60" as="geometry" />
        </mxCell>
        <mxCell id="5" style="endArrow=classic;html=1;strokeColor=#5b4636;" edge="1" parent="1" source="2" target="3">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
        <mxCell id="6" style="endArrow=classic;html=1;strokeColor=#5b4636;" edge="1" parent="1" source="3" target="4">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
```

To get the XML, open your diagram in [diagrams.net](https://app.diagrams.net), then **Extras → Edit Diagram…** and copy the XML.
