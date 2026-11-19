/* Read-only QNX Screen capture probe for the owner's ARMv7 NBT EVO.
 * Uses the public QNX 6.5 SP1 Screen ABI; dynamically loads the HU's library.
 * No windows, display properties, coding, or system configuration are changed.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <dlfcn.h>
#include <time.h>
#include <unistd.h>
#include <signal.h>
#include <sys/socket.h>
#include <poll.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <sys/syspage.h>
#include <arm_neon.h>

enum { P_FORMAT=14, P_POINTER=34, P_BUFFERS=37, P_STRIDE=44,
       P_USAGE=48, P_COUNT=59, P_DISPLAYS=60, P_NATIVE_SIZE=66, P_SIZE=40, P_BUFFER_SIZE=5 };
static int (*create_context)(void **, int);
static int (*destroy_context)(void *);
static int (*get_context_iv)(void *, int, int *);
static int (*get_context_pv)(void *, int, void **);
static int (*get_display_iv)(void *, int, int *);
static int (*create_pixmap)(void **, void *);
static int (*destroy_pixmap)(void *);
static int (*set_pixmap_iv)(void *, int, const int *);
static int (*create_pixmap_buffer)(void *);
static int (*get_pixmap_pv)(void *, int, void **);
static int (*get_buffer_pv)(void *, int, void **);
static int (*get_buffer_iv)(void *, int, int *);
static int (*read_display)(void *, void *, int, const int *, int);

#define LOAD(variable, name) do { void *symbol=dlsym(library, name); \
    memcpy(&(variable), &symbol, sizeof(variable)); \
    if (!(variable)) { fprintf(stderr,"Missing %s: %s\n", name, dlerror()); goto done; } } while (0)
#define CHECK(call) do { if ((call) != 0) { \
    fprintf(stderr,"%s failed: %d %s\n", #call, errno, strerror(errno)); goto done; } } while (0)

static double seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1000000000.0;
}
static void le32(unsigned char *p, uint32_t x) {
    p[0]=x; p[1]=x>>8; p[2]=x>>16; p[3]=x>>24;
}
static int write_bmp(FILE *file, const unsigned char *pixels, int w, int h, int stride) {
    unsigned char header[54]={0};
    uint32_t bytes=(uint32_t)w*h*4;
    int y, ok=1;
    header[0]='B'; header[1]='M'; le32(header+2,54+bytes); le32(header+10,54);
    le32(header+14,40); le32(header+18,w); le32(header+22,(uint32_t)-h);
    header[26]=1; header[28]=32; le32(header+34,bytes);
    if (fwrite(header,1,54,file)!=54) ok=0;
    for (y=0;ok && y<h;y++)
        if (fwrite(pixels+(size_t)y*stride,1,w*4,file)!=(size_t)w*4) ok=0;
    return ok ? 0 : -1;
}

static int send_all(int fd, const unsigned char *data, size_t length) {
    while (length) {
        ssize_t sent = send(fd, data, length, 0);
        if (sent < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (sent == 0) return -1;
        data += sent;
        length -= (size_t)sent;
    }
    return 0;
}

static int row_changed_scalar(const uint32_t *source,
                              const uint32_t *previous, int width) {
    int x;
    for (x=0;x<width;x++)
        if ((source[x]&0x00ffffffU)!=(previous[x]&0x00ffffffU)) return 1;
    return 0;
}

static void pack_rgb565_scalar(const uint32_t *source, uint16_t *output,
                               int width) {
    int x;
    for (x=0;x<width;x++) {
        uint32_t pixel=source[x];
        output[x]=(uint16_t)((((pixel>>19)&0x1f)<<11)
                            | (((pixel>>10)&0x3f)<<5)
                            | ((pixel>>3)&0x1f));
    }
}

static int row_changed_neon(const uint32_t *source,
                            const uint32_t *previous, int width) {
    const uint32x4_t color_mask=vdupq_n_u32(0x00ffffffU);
    int x=0;
    for (;x+16<=width;x+=16) {
        uint32x4_t d0=veorq_u32(vld1q_u32(source+x),
                               vld1q_u32(previous+x));
        uint32x4_t d1=veorq_u32(vld1q_u32(source+x+4),
                               vld1q_u32(previous+x+4));
        uint32x4_t d2=veorq_u32(vld1q_u32(source+x+8),
                               vld1q_u32(previous+x+8));
        uint32x4_t d3=veorq_u32(vld1q_u32(source+x+12),
                               vld1q_u32(previous+x+12));
        uint32x4_t diff=vandq_u32(
            vorrq_u32(vorrq_u32(d0,d1),vorrq_u32(d2,d3)),color_mask);
        uint32x2_t pair=vorr_u32(vget_low_u32(diff),vget_high_u32(diff));
        uint32x2_t reduced=vpmax_u32(pair,pair);
        if (vget_lane_u32(reduced,0)!=0)
            return 1;
    }
    return row_changed_scalar(source+x,previous+x,width-x);
}

static void pack_rgb565_neon(const uint32_t *source, uint16_t *output,
                             int width) {
    int x=0;
    for (;x+8<=width;x+=8) {
        uint8x8x4_t bgra=vld4_u8((const uint8_t *)(source+x));
        uint16x8_t red=vshlq_n_u16(vmovl_u8(bgra.val[2]),8);
        uint16x8_t green=vshlq_n_u16(vmovl_u8(bgra.val[1]),8);
        uint16x8_t blue=vshlq_n_u16(vmovl_u8(bgra.val[0]),8);
        uint16x8_t rgb565=vsriq_n_u16(red,green,5);
        rgb565=vsriq_n_u16(rgb565,blue,11);
        vst1q_u16(output+x,rgb565);
    }
    pack_rgb565_scalar(source+x,output+x,width-x);
}

static int cpu_has_neon(void) {
    const struct cpuinfo_entry *cpu=SYSPAGE_ENTRY(cpuinfo);
    return cpu && (cpu->flags&ARM_CPU_FLAG_NEON)!=0;
}

static int setup_listener(uint16_t port, const char *bind_address) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    struct sockaddr_in address;
    if (fd < 0) return -1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    if (inet_pton(AF_INET, bind_address, &address.sin_addr) != 1) {
        close(fd);
        errno = EINVAL;
        return -1;
    }
    address.sin_port = htons(port);
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) < 0 ||
        listen(fd, 1) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

int main(int argc, char **argv) {
    void *library=NULL, *ctx=NULL, *pixmap=NULL, *buffer=NULL, *pixels=NULL, *display=NULL;
    void **displays=NULL;
    int screen_rect[4]={0,0,0,0};
    int count=0, dimensions[2]={0}, capture_width=0, capture_height=0;
    int usage=8|2, format=8, stride=0, i, rc=1;
    double start, elapsed, total=0;
    int streaming=argc>1 && strcmp(argv[1],"--stream")==0;
    int tcp_streaming=argc>1 && strcmp(argv[1],"--tcp")==0;
    int width=streaming && argc>2 ? atoi(argv[2]) : 640;
    int fps=streaming && argc>3 ? atoi(argv[3]) : 5;
    int tcp_port=tcp_streaming && argc>2 ? atoi(argv[2]) : 5555;
    int tcp_fps=tcp_streaming && argc>3 ? atoi(argv[3]) : 30;
    int tcp_width=tcp_streaming && argc>4 ? atoi(argv[4]) : 0;
    int tcp_profile=0, tcp_rgb565=0;
    int tcp_options_valid=1, option_index;
    const char *tcp_bind_address=tcp_streaming && argc>5 ? argv[5] : NULL;
    const char *output=argc>1 ? argv[1] : "/tmp/idrive-direct.bmp";
    for (option_index=6;tcp_streaming && option_index<argc;option_index++) {
        if (strcmp(argv[option_index],"--profile")==0 && !tcp_profile)
            tcp_profile=1;
        else if (strcmp(argv[option_index],"--rgb565")==0 && !tcp_rgb565)
            tcp_rgb565=1;
        else {
            tcp_options_valid=0;
            break;
        }
    }
    if (streaming && (width<320 || width>1280 || fps<1 || fps>15)) {
        fprintf(stderr,"Usage: hu-screen-mirror --stream [width 320-1280] [fps 1-15]\n"); goto done;
    }
    if (tcp_streaming && (!tcp_bind_address || tcp_port<1 || tcp_port>65535 || tcp_fps<1 || tcp_fps>60 ||
                          (tcp_width!=0 && (tcp_width<320 || tcp_width>1280)) ||
                          !tcp_options_valid)) {
        fprintf(stderr,"Usage: hu-screen-mirror --tcp [port] [fps 1-60] [width 0 or 320-1280] bind-ip [--profile] [--rgb565]\n"); goto done;
    }
    library=dlopen("/lib/libscreen.so.1", RTLD_NOW|RTLD_LOCAL);
    if (!library) { fprintf(stderr,"dlopen: %s\n",dlerror()); goto done; }
    LOAD(create_context,"screen_create_context"); LOAD(destroy_context,"screen_destroy_context");
    LOAD(get_context_iv,"screen_get_context_property_iv"); LOAD(get_context_pv,"screen_get_context_property_pv");
    LOAD(get_display_iv,"screen_get_display_property_iv"); LOAD(create_pixmap,"screen_create_pixmap");
    LOAD(destroy_pixmap,"screen_destroy_pixmap"); LOAD(set_pixmap_iv,"screen_set_pixmap_property_iv");
    LOAD(create_pixmap_buffer,"screen_create_pixmap_buffer"); LOAD(get_pixmap_pv,"screen_get_pixmap_property_pv");
    LOAD(get_buffer_pv,"screen_get_buffer_property_pv"); LOAD(get_buffer_iv,"screen_get_buffer_property_iv");
    LOAD(read_display,"screen_read_display");
    CHECK(create_context(&ctx,8));
    CHECK(get_context_iv(ctx,P_COUNT,&count));
    if (count<1 || count>32) { fprintf(stderr,"Unexpected display count: %d\n",count); goto done; }
    displays=calloc(count,sizeof(void *));
    if (!displays) goto done;
    CHECK(get_context_pv(ctx,P_DISPLAYS,displays));
    for (i=0;i<count;i++) {
        int configured[2]={0,0}, native[2]={0,0};
        int configured_result, native_result;
        if (!displays[i]) { fprintf(stderr,"Display %d: null handle\n",i); continue; }
        configured_result=get_display_iv(displays[i],P_SIZE,configured);
        native_result=get_display_iv(displays[i],P_NATIVE_SIZE,native);
        fprintf(stderr,"Display %d handle %p: configured %dx%d (rc=%d), native %dx%d (rc=%d)\n",
                i,displays[i],configured[0],configured[1],configured_result,native[0],native[1],native_result);
        if (configured_result!=0 || configured[0]<1 || configured[1]<1) {
            configured[0]=native[0]; configured[1]=native[1];
        }
        if (configured[0]>dimensions[0] && configured[0]<=4096
                && configured[1]>0 && configured[1]<=2160) {
            display=displays[i]; dimensions[0]=configured[0]; dimensions[1]=configured[1];
        }
    }
    if (!display) {
        /* Known compositor size from the owner's verified LayerManager capture.
         * Buffer size only affects this probe's private pixmap; no display mode is changed. */
        display=displays[0]; dimensions[0]=1280; dimensions[1]=480;
        fprintf(stderr,"No reported resolution; testing first display with private 1280x480 capture buffer\n");
    }
    if (!display) { fprintf(stderr,"No display handle available\n"); goto done; }
    /* Preserve the full source rectangle before the TCP output size is reduced. */
    screen_rect[2]=dimensions[0];
    screen_rect[3]=dimensions[1];
    if (tcp_streaming) {
        capture_width=dimensions[0];
        capture_height=dimensions[1];
    }
    if (streaming) {
        dimensions[1]=(int)((long)dimensions[1]*width/dimensions[0]);
        dimensions[0]=width;
        if (dimensions[1]<1) dimensions[1]=1;
    }
    if (tcp_streaming && tcp_width>=320 && tcp_width<dimensions[0]) {
        dimensions[1]=(int)((long)dimensions[1]*tcp_width/dimensions[0]);
        dimensions[0]=tcp_width;
        if (dimensions[1]<1) dimensions[1]=1;
    }
    if (tcp_streaming) {
        fprintf(stderr,"Capture source: %dx%d, buffer/stream: %dx%d\n",
                capture_width,capture_height,dimensions[0],dimensions[1]);
        fprintf(stderr,"Timing profile: %s\n",tcp_profile ? "enabled" : "disabled");
        if (tcp_rgb565)
            fprintf(stderr,"RGB565 packing: %s\n",
                    cpu_has_neon() ? "ARM NEON" : "scalar fallback");
    } else {
        fprintf(stderr,"Capture buffer: %dx%d\n",dimensions[0],dimensions[1]);
    }
    CHECK(create_pixmap(&pixmap,ctx));
    CHECK(set_pixmap_iv(pixmap,P_FORMAT,&format));
    CHECK(set_pixmap_iv(pixmap,P_USAGE,&usage));
    CHECK(set_pixmap_iv(pixmap,P_BUFFER_SIZE,dimensions));
    CHECK(create_pixmap_buffer(pixmap));
    CHECK(get_pixmap_pv(pixmap,P_BUFFERS,&buffer));
    CHECK(get_buffer_pv(buffer,P_POINTER,&pixels));
    CHECK(get_buffer_iv(buffer,P_STRIDE,&stride));
    if (!pixels || stride<dimensions[0]*4 || stride>65536) goto done;
    if (tcp_streaming) {
        int server_fd=-1, client_fd=-1, nodelay=1, sndbuf=512*1024;
        uint32_t *previous=NULL;
        unsigned char *current=NULL, *packet=NULL;
        size_t row_payload_bytes=(size_t)dimensions[0]
            *(tcp_rgb565 ? 2U : 3U);
        size_t row_source=(size_t)dimensions[0]*sizeof(*previous);
        int use_neon=tcp_rgb565 && cpu_has_neon();
        double interval=1.0/tcp_fps;
        int force_full=1;
        double profile_start=0, capture_time=0, pack_time=0, send_time=0;
        unsigned profile_frames=0, profile_rows=0;
        signal(SIGPIPE, SIG_IGN);
        if (((uintptr_t)pixels % sizeof(uint32_t)) != 0
                || stride % sizeof(uint32_t) != 0) {
            fprintf(stderr,"Capture buffer is not 32-bit aligned\n");
            goto tcp_done;
        }
        previous=calloc((size_t)dimensions[0]*(size_t)dimensions[1],
                        sizeof(*previous));
        current=malloc(row_payload_bytes);
        packet=malloc(8+(size_t)dimensions[1]*(2+row_payload_bytes));
        if (!previous || !current || !packet) goto tcp_done;
        server_fd=setup_listener((uint16_t)tcp_port,tcp_bind_address);
        if (server_fd<0) { perror("setup_listener"); goto tcp_done; }
        fprintf(stdout,"READY %d %d %u\n",dimensions[0],dimensions[1],tcp_port);
        fflush(stdout);
        client_fd=accept(server_fd,NULL,NULL);
        close(server_fd);
        server_fd=-1;
        if (client_fd<0) { perror("accept"); goto tcp_done; }
        profile_start=seconds();
        setsockopt(client_fd,IPPROTO_TCP,TCP_NODELAY,&nodelay,sizeof(nodelay));
        setsockopt(client_fd,SOL_SOCKET,SO_SNDBUF,&sndbuf,sizeof(sndbuf));
        for (;;) {
            double frame_start=seconds();
            double stage_start=0;
            uint16_t changed_rows=0;
            size_t offset=8;
            struct pollfd socket_state;
            int writable;
            int y;
            socket_state.fd=client_fd;
            socket_state.events=POLLOUT;
            socket_state.revents=0;
            writable=poll(&socket_state,1,0);
            if (writable<0) {
                if (errno==EINTR) continue;
                perror("poll");
                break;
            }
            if (socket_state.revents&(POLLERR|POLLHUP|POLLNVAL)) break;
            if (writable==0 || !(socket_state.revents&POLLOUT)) {
                usleep(1000);
                continue;
            }
            if (tcp_profile) stage_start=seconds();
            CHECK(read_display(display,buffer,1,screen_rect,0));
            if (tcp_profile) {
                capture_time+=seconds()-stage_start;
                stage_start=seconds();
            }
            for (y=0;y<dimensions[1];y++) {
                const unsigned char *source=(const unsigned char *)pixels
                    +(size_t)y*stride;
                const uint32_t *source_pixel=(const uint32_t *)source;
                uint32_t *prior=previous+(size_t)y*dimensions[0];
                int row_changed=force_full;
                if (!row_changed)
                    row_changed=use_neon
                        ? row_changed_neon(source_pixel,prior,dimensions[0])
                        : row_changed_scalar(source_pixel,prior,dimensions[0]);
                if (row_changed) {
                    memcpy(prior,source,row_source);
                    if (tcp_rgb565) {
                        if (use_neon)
                            pack_rgb565_neon(source_pixel,(uint16_t *)current,
                                             dimensions[0]);
                        else
                            pack_rgb565_scalar(source_pixel,(uint16_t *)current,
                                               dimensions[0]);
                    } else {
                        int x;
                        for (x=0;x<dimensions[0];x++) {
                            uint32_t pixel=source_pixel[x];
                            current[(size_t)x*3]=(unsigned char)(pixel>>16);
                            current[(size_t)x*3+1]=(unsigned char)(pixel>>8);
                            current[(size_t)x*3+2]=(unsigned char)pixel;
                        }
                    }
                    packet[offset]=(unsigned char)(y&0xff);
                    packet[offset+1]=(unsigned char)((y>>8)&0xff);
                    memcpy(packet+offset+2,current,row_payload_bytes);
                    offset+=2+row_payload_bytes;
                    changed_rows++;
                }
            }
            if (tcp_profile) {
                pack_time+=seconds()-stage_start;
                stage_start=seconds();
            }
            if (tcp_profile) profile_rows+=changed_rows;
            force_full=0;
            packet[0]='D';
            packet[1]=tcp_rgb565
                ? (tcp_profile ? 'A' : '8')
                : (tcp_profile ? '6' : '4');
            packet[2]=(unsigned char)(dimensions[0]&0xff);
            packet[3]=(unsigned char)((dimensions[0]>>8)&0xff);
            packet[4]=(unsigned char)(dimensions[1]&0xff);
            packet[5]=(unsigned char)((dimensions[1]>>8)&0xff);
            packet[6]=(unsigned char)(changed_rows&0xff);
            packet[7]=(unsigned char)((changed_rows>>8)&0xff);
            if (tcp_profile) stage_start=seconds();
            if (send_all(client_fd,packet,offset)!=0) break;
            if (tcp_profile) {
                send_time+=seconds()-stage_start;
                profile_frames++;
                elapsed=seconds()-profile_start;
                if (elapsed>=2.0) {
                    fprintf(stderr,"PERF fps=%.1f rows=%.1f capture=%.1fms pack=%.1fms send=%.1fms\n",
                            profile_frames/elapsed,
                            (double)profile_rows/profile_frames,
                            capture_time*1000/profile_frames,
                            pack_time*1000/profile_frames,
                            send_time*1000/profile_frames);
                    fflush(stderr);
                    profile_start=seconds();
                    capture_time=pack_time=send_time=0;
                    profile_frames=0;
                    profile_rows=0;
                }
            }
            elapsed=seconds()-frame_start;
            if (elapsed<interval) usleep((unsigned)((interval-elapsed)*1000000));
        }
        rc=0;
tcp_done:
        if (client_fd>=0) close(client_fd);
        if (server_fd>=0) close(server_fd);
        free(packet);
        free(current);
        free(previous);
        goto done;
    }
    if (streaming) {
        fprintf(stderr,"Direct mirror streaming at up to %d FPS; Ctrl+C on PC stops it\n",fps);
        for (;;) {
            start=seconds();
            CHECK(read_display(display,buffer,0,NULL,0));
            if (fprintf(stdout,"IDRIVE_FRAME %u\n",54+(unsigned)dimensions[0]*dimensions[1]*4)<0) goto done;
            CHECK(write_bmp(stdout,pixels,dimensions[0],dimensions[1],stride));
            if (fflush(stdout)!=0) goto done;
            elapsed=seconds()-start;
            if (elapsed<1.0/fps) usleep((unsigned)((1.0/fps-elapsed)*1000000));
        }
    }
    for (i=0;i<5;i++) {
        start=seconds();
        CHECK(read_display(display,buffer,0,NULL,0));
        elapsed=seconds()-start; total+=elapsed;
        fprintf(stderr,"Direct capture %d: %.3f seconds\n",i+1,elapsed);
        usleep(100000);
    }
    fprintf(stderr,"Average direct capture: %.3f seconds\n",total/5);
    {
        FILE *image=fopen(output,"wb");
        int image_result;
        if (!image) { perror("fopen"); goto done; }
        image_result=write_bmp(image,pixels,dimensions[0],dimensions[1],stride);
        if (fclose(image)!=0) image_result=-1;
        CHECK(image_result);
    }
    fprintf(stderr,"Saved %s\n",output);
    rc=0;
done:
    if (pixmap && destroy_pixmap) destroy_pixmap(pixmap);
    if (ctx && destroy_context) destroy_context(ctx);
    free(displays);
    if (library) dlclose(library);
    return rc;
}
