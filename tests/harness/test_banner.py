from entrabot.harness import banner


def test_render_single_line_block_glyphs():
    rows = banner.render()
    # one-line wordmark: 5 glyph rows + a blank row top and bottom
    assert len(rows) == 7
    text = "".join(t for row in rows for t, _ in row)
    assert "█" in text  # block-font glyphs are present


def test_shaded_two_section_colors_with_shadow():
    rows = banner.render()
    colors = {color for row in rows for _, color in row if color}
    # ENTRA blue (body + bright top), BOT pink (body + bright top), plus a drop shadow
    assert {"entra", "entra_hi", "bot", "bot_hi", "shadow"} <= colors
