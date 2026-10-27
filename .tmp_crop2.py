
from PIL import Image
im = Image.open("data/logs/cdp_hr3.png")
crop = im.crop((860, 690, 1120, 790)).resize((260*3, 100*3), Image.LANCZOS)
crop.save("data/logs/crop_hr2.png")
print(im.size, "saved")
