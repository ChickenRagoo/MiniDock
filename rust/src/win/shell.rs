//! Оболочка Windows: иконки (IShellItemImageFactory), ярлыки, описания программ, запуск, «Пуск».

use windows::core::{Interface, HSTRING, PCWSTR, PWSTR};
use windows::Win32::Foundation::{HWND, SIZE};
use windows::Win32::Graphics::Gdi::{
    DeleteObject, GetDC, GetDIBits, GetObjectW, ReleaseDC, BITMAP, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS, HBITMAP,
};
use windows::Win32::Storage::FileSystem::{GetFileVersionInfoSizeW, GetFileVersionInfoW, VerQueryValueW};
use windows::Win32::System::Com::*;
use windows::Win32::UI::Shell::Common::ITEMIDLIST;
use windows::Win32::UI::Shell::*;
use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

use crate::gfx::Image;

pub fn com_init() {
    unsafe {
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    }
}

/// HBITMAP (32 бита) → картинка BGRA с предумноженной альфой.
pub fn hbitmap_to_image(hbmp: HBITMAP) -> Option<Image> {
    unsafe {
        let mut bm = BITMAP::default();
        if GetObjectW(hbmp.into(), std::mem::size_of::<BITMAP>() as i32, Some(&mut bm as *mut _ as *mut _)) == 0 {
            return None;
        }
        let (w, h) = (bm.bmWidth, bm.bmHeight.abs());
        if w <= 0 || h <= 0 {
            return None;
        }
        let mut bmi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: -h,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut data = vec![0u8; (w * h * 4) as usize];
        let dc = GetDC(None);
        let n = GetDIBits(dc, hbmp, 0, h as u32, Some(data.as_mut_ptr() as *mut _), &mut bmi, DIB_RGB_COLORS);
        ReleaseDC(None, dc);
        if n == 0 {
            return None;
        }
        fix_alpha(&mut data);
        Image::from_bgra_premul(w as u32, h as u32, data)
    }
}

/// Привести буфер к предумноженной альфе: картинки без альфы — непрозрачные, не предумноженные — умножить.
pub fn fix_alpha(data: &mut [u8]) {
    let has_alpha = data.chunks(4).any(|p| p[3] != 0);
    if !has_alpha {
        for p in data.chunks_mut(4) {
            p[3] = 255;
        }
        return;
    }
    let premul = data.chunks(4).all(|p| p[0] <= p[3] && p[1] <= p[3] && p[2] <= p[3]);
    if !premul {
        for p in data.chunks_mut(4) {
            let a = p[3] as u32;
            p[0] = (p[0] as u32 * a / 255) as u8;
            p[1] = (p[1] as u32 * a / 255) as u8;
            p[2] = (p[2] as u32 * a / 255) as u8;
        }
    }
}

fn item_image(item: &IShellItem, size: i32, icon_only: bool) -> Option<Image> {
    unsafe {
        let f: IShellItemImageFactory = item.cast().ok()?;
        let mut flags = SIIGBF_BIGGERSIZEOK;
        if icon_only {
            flags |= SIIGBF_ICONONLY;
        }
        let hbmp = f.GetImage(SIZE { cx: size, cy: size }, flags).ok()?;
        let img = hbitmap_to_image(hbmp);
        let _ = DeleteObject(hbmp.into());
        let img = img?;
        if img.w as i32 > size || img.h as i32 > size {
            let k = size as f32 / img.w.max(img.h) as f32;
            return Some(img.scaled((img.w as f32 * k).round() as u32, (img.h as f32 * k).round() as u32));
        }
        Some(img)
    }
}

/// Иконка файла / программы / папки / «shell:…» нужного размера (по умолчанию 128 px).
pub fn file_icon(path: &str, size: i32) -> Option<Image> {
    let mut target = path.to_string();
    if path.to_lowercase().ends_with(".lnk") {
        if let Some((t, icon_file, idx)) = resolve_lnk(path) {
            if let Some(img) = (!icon_file.is_empty()).then(|| extract_icon(&icon_file, idx, size)).flatten() {
                return Some(img);
            }
            if !t.is_empty() && std::path::Path::new(&t).exists() {
                target = t;
            }
        }
    }
    unsafe {
        let item: IShellItem = SHCreateItemFromParsingName(&HSTRING::from(target.as_str()), None).ok()?;
        item_image(&item, size, true)
    }
}

/// Иконка по номеру из exe/dll (как PrivateExtractIcons).
pub fn extract_icon(file: &str, index: i32, size: i32) -> Option<Image> {
    use windows::Win32::UI::WindowsAndMessaging::{DestroyIcon, GetIconInfo, PrivateExtractIconsW, HICON, ICONINFO};
    unsafe {
        let path = crate::win::sys::wstr(file);
        let mut name = [0u16; 260];
        let n = path.len().min(259);
        name[..n].copy_from_slice(&path[..n]);
        let mut icons = [HICON::default(); 1];
        let mut ids = [0u32; 1];
        let got = PrivateExtractIconsW(&name, index, size, size, Some(&mut icons), Some(ids.as_mut_ptr()), 0);
        if got == 0 || got == u32::MAX || icons[0].is_invalid() {
            return None;
        }
        let mut info = ICONINFO::default();
        let mut out = None;
        if GetIconInfo(icons[0], &mut info).is_ok() {
            out = hbitmap_to_image(info.hbmColor);
            let _ = DeleteObject(info.hbmColor.into());
            let _ = DeleteObject(info.hbmMask.into());
        }
        let _ = DestroyIcon(icons[0]);
        out
    }
}

/// Ярлык → (цель, файл иконки, номер иконки).
pub fn resolve_lnk(path: &str) -> Option<(String, String, i32)> {
    unsafe {
        let link: IShellLinkW = CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER).ok()?;
        let pf: IPersistFile = link.cast().ok()?;
        pf.Load(&HSTRING::from(path), STGM_READ).ok()?;
        let mut buf = [0u16; 1024];
        let _ = link.GetPath(&mut buf, std::ptr::null_mut(), SLGP_RAWPATH.0 as u32);
        let target = expand_env(&from_wide(&buf));
        let mut ibuf = [0u16; 1024];
        let mut idx = 0i32;
        let _ = link.GetIconLocation(&mut ibuf, &mut idx);
        let mut icon = expand_env(&from_wide(&ibuf));
        if icon.is_empty() || !std::path::Path::new(&icon).exists() {
            icon = String::new();
            idx = 0;
        }
        Some((target, icon, idx))
    }
}

pub fn lnk_args(path: &str) -> String {
    unsafe {
        let Ok(link) = CoCreateInstance::<_, IShellLinkW>(&ShellLink, None, CLSCTX_INPROC_SERVER) else { return String::new() };
        let Ok(pf) = link.cast::<IPersistFile>() else { return String::new() };
        if pf.Load(&HSTRING::from(path), STGM_READ).is_err() {
            return String::new();
        }
        let mut buf = [0u16; 1024];
        let _ = link.GetArguments(&mut buf);
        from_wide(&buf)
    }
}

pub fn from_wide(buf: &[u16]) -> String {
    let n = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    String::from_utf16_lossy(&buf[..n])
}

pub fn expand_env(s: &str) -> String {
    use windows::Win32::System::Environment::ExpandEnvironmentStringsW;
    if !s.contains('%') {
        return s.to_string();
    }
    let src = crate::win::sys::wstr(s);
    let mut buf = vec![0u16; 2048];
    let n = unsafe { ExpandEnvironmentStringsW(PCWSTR(src.as_ptr()), Some(&mut buf)) };
    if n == 0 {
        return s.to_string();
    }
    from_wide(&buf)
}

/// Описание программы из её ресурсов (FileDescription), например «Telegram Desktop».
pub fn exe_description(exe: &str) -> Option<String> {
    unsafe {
        let p = HSTRING::from(exe);
        let size = GetFileVersionInfoSizeW(&p, None);
        if size == 0 {
            return None;
        }
        let mut data = vec![0u8; size as usize];
        GetFileVersionInfoW(&p, None, size, data.as_mut_ptr() as *mut _).ok()?;
        let mut ptr = std::ptr::null_mut();
        let mut len = 0u32;
        if !VerQueryValueW(data.as_ptr() as *const _, &HSTRING::from("\\VarFileInfo\\Translation"), &mut ptr, &mut len).as_bool() || len < 4 {
            return None;
        }
        let tr = *(ptr as *const [u16; 2]);
        let key = format!("\\StringFileInfo\\{:04x}{:04x}\\FileDescription", tr[0], tr[1]);
        if !VerQueryValueW(data.as_ptr() as *const _, &HSTRING::from(key), &mut ptr, &mut len).as_bool() || len == 0 {
            return None;
        }
        let s = String::from_utf16_lossy(std::slice::from_raw_parts(ptr as *const u16, len as usize));
        let s = s.trim_matches(char::from(0)).trim().to_string();
        (!s.is_empty()).then_some(s)
    }
}

pub fn is_elevated() -> bool {
    unsafe { IsUserAnAdmin().as_bool() }
}

/// Запустить программу или файл как обычный пользователь (из-под администратора — через Проводник).
pub fn launch(path: &str, args: &str, dir: Option<&str>) -> bool {
    if is_elevated() && args.is_empty() {
        return spawn("explorer.exe", &[path]);
    }
    unsafe {
        let p = HSTRING::from(path);
        let a = HSTRING::from(args);
        let d = dir.map(HSTRING::from);
        let r = ShellExecuteW(
            None,
            &HSTRING::from("open"),
            &p,
            if args.is_empty() { PCWSTR::null() } else { PCWSTR(a.as_ptr()) },
            d.as_ref().map(|d| PCWSTR(d.as_ptr())).unwrap_or(PCWSTR::null()),
            SW_SHOWNORMAL,
        );
        r.0 as isize > 32
    }
}

pub fn spawn(exe: &str, args: &[&str]) -> bool {
    use std::os::windows::process::CommandExt;
    std::process::Command::new(exe).args(args).creation_flags(0x08000000).spawn().is_ok()
}

pub fn open_recycle_bin() {
    spawn("explorer.exe", &["shell:RecycleBinFolder"]);
}

pub fn show_in_explorer(path: &str) {
    spawn("explorer.exe", &["/select,", path]);
}

pub fn recycle_bin_icon(size: i32) -> Option<Image> {
    unsafe {
        let item: IShellItem = SHCreateItemFromParsingName(&HSTRING::from("shell:RecycleBinFolder"), None).ok()?;
        item_image(&item, size, true)
    }
    .or_else(|| extract_icon(&format!("{}\\System32\\shell32.dll", crate::win::sys::system_root()), 31, size))
}

/// Приложение из меню «Пуск» (включая программы Store).
#[derive(Clone, Debug)]
pub struct StartApp {
    pub name: String,
    /// «shell:AppsFolder\…» — так его и запускаем
    pub parse: String,
}

/// Все приложения «Пуска» (shell:AppsFolder). Вызывать в фоновом потоке (COM инициализируется там).
pub fn enum_start_apps() -> Vec<StartApp> {
    let mut out = Vec::new();
    unsafe {
        let Ok(folder) = SHCreateItemFromParsingName::<_, _, IShellItem>(&HSTRING::from("shell:AppsFolder"), None) else { return out };
        let Ok(en) = folder.BindToHandler::<_, IEnumShellItems>(None, &BHID_EnumItems) else { return out };
        loop {
            let mut items = [None];
            let mut got = 0u32;
            if en.Next(&mut items, Some(&mut got)).is_err() || got == 0 {
                break;
            }
            let Some(item) = items[0].take() else { break };
            let name = item.GetDisplayName(SIGDN_NORMALDISPLAY).map(|p| take_pwstr(p)).unwrap_or_default();
            let parse = item.GetDisplayName(SIGDN_PARENTRELATIVEPARSING).map(|p| take_pwstr(p)).unwrap_or_default();
            if !name.is_empty() && !parse.is_empty() {
                out.push(StartApp { name, parse });
            }
        }
    }
    out
}

/// Иконка приложения «Пуска» по его имени разбора.
pub fn start_app_icon(parse: &str, size: i32) -> Option<Image> {
    unsafe {
        let item: IShellItem = SHCreateItemFromParsingName(&HSTRING::from(format!("shell:AppsFolder\\{parse}")), None).ok()?;
        item_image(&item, size, true)
    }
}

pub fn launch_start_app(parse: &str) -> bool {
    spawn("explorer.exe", &[&format!("shell:AppsFolder\\{parse}")])
}

pub fn take_pwstr(p: PWSTR) -> String {
    unsafe {
        let s = p.to_string().unwrap_or_default();
        CoTaskMemFree(Some(p.0 as *const _));
        s
    }
}

pub fn known_folder(id: &windows::core::GUID) -> String {
    unsafe { SHGetKnownFolderPath(id, KNOWN_FOLDER_FLAG(0), None).map(|p| take_pwstr(p)).unwrap_or_default() }
}

pub fn appdata() -> String {
    std::env::var("APPDATA").unwrap_or_default()
}

/// Диалог выбора файлов (модальный — вызывать без захваченного состояния приложения).
pub fn open_files_dialog(owner: HWND, title: &str, filter_name: &str, filter: &str, multi: bool) -> Vec<String> {
    let mut out = Vec::new();
    unsafe {
        let Ok(dlg) = CoCreateInstance::<_, IFileOpenDialog>(&FileOpenDialog, None, CLSCTX_INPROC_SERVER) else { return out };
        let mut opts = dlg.GetOptions().unwrap_or_default() | FOS_FORCEFILESYSTEM | FOS_NODEREFERENCELINKS;
        if multi {
            opts |= FOS_ALLOWMULTISELECT;
        }
        let _ = dlg.SetOptions(opts);
        let _ = dlg.SetTitle(&HSTRING::from(title));
        let n1 = HSTRING::from(filter_name);
        let f1 = HSTRING::from(filter);
        let n2 = HSTRING::from("Все файлы");
        let f2 = HSTRING::from("*.*");
        let specs = [
            Common::COMDLG_FILTERSPEC { pszName: PCWSTR(n1.as_ptr()), pszSpec: PCWSTR(f1.as_ptr()) },
            Common::COMDLG_FILTERSPEC { pszName: PCWSTR(n2.as_ptr()), pszSpec: PCWSTR(f2.as_ptr()) },
        ];
        let _ = dlg.SetFileTypes(&specs);
        if dlg.Show(Some(owner)).is_err() {
            return out;
        }
        if let Ok(arr) = dlg.GetResults() {
            let n = arr.GetCount().unwrap_or(0);
            for i in 0..n {
                if let Ok(it) = arr.GetItemAt(i) {
                    if let Ok(p) = it.GetDisplayName(SIGDN_FILESYSPATH) {
                        out.push(take_pwstr(p));
                    }
                }
            }
        }
    }
    out
}

pub fn _unused(_: *const ITEMIDLIST) {}
