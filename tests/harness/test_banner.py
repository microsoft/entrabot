from entrabot.harness import banner


def test_render_has_rows_and_block_glyphs():
    rows = banner.render()
    assert len(rows) > 8
    text = "".join(t for row in rows for t, _ in row)
    assert "█" in text  # block-font glyphs are present


def test_two_tone_colors_present():
    rows = banner.render()
    colors = {color for row in rows for _, color in row}
    # ENTRA is blue, BOT is pink (magenta); shadow is gray
    assert "blue" in colors or "bright_blue" in colors
    assert "magenta" in colors or "bright_magenta" in colors
