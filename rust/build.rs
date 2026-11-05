fn main() {
    embed_resource::compile("res/minidock.rc", embed_resource::NONE).manifest_optional().unwrap();
    println!("cargo:rerun-if-changed=res/minidock.rc");
    println!("cargo:rerun-if-changed=res/minidock.manifest");
}
