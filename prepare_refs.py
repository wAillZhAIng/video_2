"""
准备 D:\人物一致性 直通工作流所需参考图
- 画布目标：768x1344 (9:16 竖屏)
- 来源：C:\Users\MSI\Desktop\测试\车灯 的实际图片
"""
from PIL import Image, ImageOps
import os, shutil

SRC  = r"C:\Users\MSI\Desktop\测试\车灯"
DST  = r"D:\人物一致性\.local-runtime\inference\input"
W, H = 768, 1344   # 9:16 竖屏

def fit(src_path, dst_path):
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im = ImageOps.fit(im, (W, H), Image.LANCZOS)
        im.save(dst_path, quality=95)
        print(f"  {os.path.basename(dst_path)}: {im.size}")

os.makedirs(DST, exist_ok=True)

# 根据 image-001/002/003.json 的图片分析确认内容：
# 20261124-021634.jpg (1792x2400) → 人持车，尾灯亮橙色箭头投地 → left_arrow_ref
# 20261124-021628.jpg (1792x2400) → 人持车，尾灯亮橙色箭头投地 → right_arrow_ref
# 20261123-224604.jpg (1920x1920) → 产品平铺白底全家福图      → product_main

fit(rf"{SRC}\20261124-021634.jpg", rf"{DST}\product_left_arrow.jpg")
fit(rf"{SRC}\20261124-021628.jpg", rf"{DST}\product_right_arrow.jpg")
fit(rf"{SRC}\20261123-224604.jpg", rf"{DST}\product_main.jpg")

# char_ref: 目前用 Nomi 的 char_ref.png，重新裁切为 9:16
nomi_char = r"E:\openfilm\Jellyfish\nomi-local\input\char_ref.png"
fit(nomi_char, rf"{DST}\char_ref.png")

# 删除旧的横向版本 README（内容过时）
old_readme = rf"{DST}\DIRECT_WORKFLOW_README.txt"
if os.path.exists(old_readme):
    os.remove(old_readme)

# 写新 README
with open(rf"{DST}\README.txt", "w", encoding="utf-8") as f:
    f.write(
        "Direct workflow reference images — 768x1344 (9:16)\n"
        "  char_ref.png            character identity\n"
        "  product_main.jpg        product flat-lay white-bg (20261123-224604.jpg)\n"
        "  product_left_arrow.jpg  left arrow lit scene (20261124-021634.jpg)\n"
        "  product_right_arrow.jpg right arrow lit scene (20261124-021628.jpg)\n"
    )

print("Done.")
