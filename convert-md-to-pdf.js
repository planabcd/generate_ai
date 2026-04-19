const fs = require('fs');
const path = require('path');
const PDFDocument = require('pdfkit');

const inputPath = path.resolve(process.argv[2] || '需求文档.md');
const outputPath = path.resolve(process.argv[3] || '需求文档.pdf');

const fontPath = path.resolve(
  'node_modules/@fontsource/noto-sans-sc/files/noto-sans-sc-chinese-simplified-400-normal.woff'
);

if (!fs.existsSync(inputPath)) {
  console.error(`Input file not found: ${inputPath}`);
  process.exit(1);
}
if (!fs.existsSync(fontPath)) {
  console.error(`Font file not found: ${fontPath}`);
  process.exit(1);
}

const md = fs.readFileSync(inputPath, 'utf8').replace(/\r\n/g, '\n');
const lines = md.split('\n');

const doc = new PDFDocument({
  size: 'A4',
  margins: { top: 56, bottom: 56, left: 56, right: 56 },
  info: {
    Title: path.basename(inputPath),
    Author: 'Auto Generator'
  }
});

const stream = fs.createWriteStream(outputPath);
doc.pipe(stream);

doc.registerFont('NotoSC', fontPath);
doc.font('NotoSC').fontSize(12);

for (const rawLine of lines) {
  const line = rawLine.replace(/\t/g, '    ');

  if (line.trim() === '') {
    doc.moveDown(0.6);
    continue;
  }

  if (line.startsWith('# ')) {
    doc.moveDown(0.3);
    doc.font('NotoSC').fontSize(18).text(line.slice(2).trim(), { align: 'left' });
    doc.moveDown(0.4);
    doc.fontSize(12);
    continue;
  }

  if (line.startsWith('## ')) {
    doc.moveDown(0.2);
    doc.font('NotoSC').fontSize(14).text(line.slice(3).trim(), { align: 'left' });
    doc.moveDown(0.2);
    doc.fontSize(12);
    continue;
  }

  if (line.startsWith('### ')) {
    doc.font('NotoSC').fontSize(12).text(line.slice(4).trim(), { underline: true });
    continue;
  }

  const normalized = line
    .replace(/^[-*]\s+/, '• ')
    .replace(/`([^`]+)`/g, '$1');

  doc.font('NotoSC').fontSize(12).text(normalized, {
    align: 'left',
    lineGap: 3
  });
}

doc.end();

stream.on('finish', () => {
  console.log(`PDF generated: ${outputPath}`);
});
