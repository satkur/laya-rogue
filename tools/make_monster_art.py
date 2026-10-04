"""Monster art for the viewer: picks one picture per Rogue letter from the free monster collection by とり夫
(see THIRD_PARTY_NOTICES.md), crops the black margin, and writes 512 px WebP files to static/monsters/.

    uv run --with pillow tools/make_monster_art.py <folder of the unzipped collection>
"""
import sys
from pathlib import Path

from PIL import Image

ART = {  # letter -> file in the collection
    "A": "01 Monster basic v21/Identified Monster Ver.21/Marine Drop.jpg",
    "B": "2020 追加モンスター ver6/Imp A.png",
    "C": "01 Monster basic v21/Unidentified Ver.20/Horse&Knight.jpg",
    "D": "01 Monster basic v21/Identified Monster Ver.21/Fire Dragon.jpg",
    "E": "01 Monster basic v21/Identified Monster Ver.21/Cockatrice.jpg",
    "F": "01 Monster basic v21/Identified Monster Ver.21/Strangler Vine A.jpg",
    "G": "2017-2018 追加モンスター ver5/Griffin A.jpg",
    "H": "2017-2018 追加モンスター ver5/Hob Goblin A.jpg",
    "I": "2020 追加モンスター ver6/Frozen Orb A.png",
    "J": "01 Monster basic v21/Identified Monster Ver.21/Wyvern.jpg",
    "K": "01 Monster basic v21/Identified Monster Ver.21/Meat Pecker.jpg",
    "L": "2021 追加モンスター ver5/Goblin Merchant A.png",
    "M": "01 Monster basic v21/Identified Monster Ver.21/Earth Medusa.jpg",
    "N": "2019 追加モンスター ver7/Water Nymph A.png",
    "O": "2019 追加モンスター ver7/Orc Scale A.png",
    "P": "01 Monster basic v21/Identified Monster Ver.21/Ghost.jpg",
    "Q": "01 Monster basic v21/Identified Monster Ver.21/Sleipnir.jpg",
    "R": "2021 追加モンスター ver5/Snake & Orb A.png",
    "S": "01 Monster basic v21/Identified Monster Ver.21/Constrictor.jpg",
    "T": "01 Monster basic v21/Identified Monster Ver.21/Troll.jpg",
    "U": "モンスター色差分U/Unicorn F.jpg",
    "V": "01 Monster basic v21/Identified Monster Ver.21/Vampire Lord.jpg",
    "W": "01 Monster basic v21/Identified Monster Ver.21/Wraith.jpg",
    "X": "01 Monster basic v21/Identified Monster Ver.21/Treasure Box Re A.png",
    "Y": "2021 追加モンスター ver5/Were Wolf A.png",
    "Z": "01 Monster basic v21/Identified Monster Ver.21/Zombie.jpg",
}
SIZE, PAD = 512, 0.04


def convert(src, dst):
    im = Image.open(src).convert("RGB")
    box = im.convert("L").point(lambda v: 255 if v > 24 else 0).getbbox() or (0, 0, *im.size)
    im = im.crop(box)
    side = round(max(im.size) * (1 + 2 * PAD))
    sq = Image.new("RGB", (side, side))
    sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    sq.resize((SIZE, SIZE), Image.LANCZOS).save(dst, "WEBP", quality=80, method=6)


def main():
    root = Path(sys.argv[1])
    out = Path(__file__).parent.parent / "static" / "monsters"
    out.mkdir(parents=True, exist_ok=True)
    for ch, rel in ART.items():
        convert(root / rel, out / f"{ch}.webp")
    print(f"{len(ART)} files, {sum(p.stat().st_size for p in out.glob('*.webp')) / 1e6:.2f} MB -> {out}")


if __name__ == "__main__":
    main()
