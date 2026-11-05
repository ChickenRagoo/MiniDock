//! Картинки с диска (фоны частей интерфейса, обложки): WIC — любой формат Windows (PNG, JPG, WebP, HEIC…).

use windows::core::HSTRING;
use windows::Win32::Graphics::Imaging::*;
use windows::Win32::System::Com::{CoCreateInstance, CLSCTX_INPROC_SERVER};

use crate::gfx::Image;

fn factory() -> Option<IWICImagingFactory> {
    unsafe { CoCreateInstance(&CLSID_WICImagingFactory, None, CLSCTX_INPROC_SERVER).ok() }
}

fn decode(src: &IWICBitmapSource, max: u32) -> Option<Image> {
    unsafe {
        let f = factory()?;
        let (mut w, mut h) = (0u32, 0u32);
        src.GetSize(&mut w, &mut h).ok()?;
        let mut source: IWICBitmapSource = src.clone();
        if w.max(h) > max && max > 0 {
            let k = max as f32 / w.max(h) as f32;
            let (nw, nh) = (((w as f32) * k).round().max(1.0) as u32, ((h as f32) * k).round().max(1.0) as u32);
            let sc = f.CreateBitmapScaler().ok()?;
            sc.Initialize(&source, nw, nh, WICBitmapInterpolationModeFant).ok()?;
            source = sc.into();
            w = nw;
            h = nh;
        }
        let conv = f.CreateFormatConverter().ok()?;
        conv.Initialize(&source, &GUID_WICPixelFormat32bppPBGRA, WICBitmapDitherTypeNone, None, 0.0, WICBitmapPaletteTypeCustom).ok()?;
        let mut data = vec![0u8; (w * h * 4) as usize];
        conv.CopyPixels(std::ptr::null(), w * 4, &mut data).ok()?;
        Image::from_bgra_premul(w, h, data)
    }
}

/// Загрузить файл, уменьшив до max px по большей стороне.
pub fn load(path: &str, max: u32) -> Option<Image> {
    unsafe {
        let f = factory()?;
        let dec = f.CreateDecoderFromFilename(&HSTRING::from(path), None, windows::Win32::Foundation::GENERIC_READ, WICDecodeMetadataCacheOnDemand).ok()?;
        let frame = dec.GetFrame(0).ok()?;
        decode(&frame.into(), max)
    }
}

/// Картинка из байтов в памяти (обложки плееров).
pub fn load_bytes(bytes: &[u8], max: u32) -> Option<Image> {
    unsafe {
        let f = factory()?;
        let stream = f.CreateStream().ok()?;
        stream.InitializeFromMemory(bytes).ok()?;
        let dec = f.CreateDecoderFromStream(&stream, std::ptr::null(), WICDecodeMetadataCacheOnDemand).ok()?;
        let frame = dec.GetFrame(0).ok()?;
        decode(&frame.into(), max)
    }
}
