import sharp from "sharp";
import { renderSVG } from "uqr";

// a png, because tor browser's safest mode blocks svg
export const qrCode = async (text: string): Promise<string> => {
  const svg = Buffer.from(renderSVG(text, { border: 4 }));
  const png = await sharp(svg).png({ palette: true, colours: 2 }).toBuffer();
  return `data:image/png;base64,${png.toString("base64")}`;
};
