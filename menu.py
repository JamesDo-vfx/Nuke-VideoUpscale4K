import nuke


def upscale_video():
    from nuke_video_upscale_node import create_node

    with nuke.lastHitGroup():
        create_node().showControlPanel()


nuke.menu("Nodes").addCommand("Image/Video Upscale 4K", upscale_video)
