import { useEffect } from 'react'

type RuntimeStatus={local_runtime:boolean;automatic_shutdown:boolean}

export function useRuntimeSession(){
  useEffect(()=>{
    let disposed=false
    let enabled=false
    let socket:WebSocket|undefined
    let reconnectTimer:number|undefined
    let heartbeatTimer:number|undefined

    function clearTimers(){
      if(reconnectTimer!==undefined)window.clearTimeout(reconnectTimer)
      if(heartbeatTimer!==undefined)window.clearInterval(heartbeatTimer)
      reconnectTimer=undefined
      heartbeatTimer=undefined
    }
    function scheduleReconnect(){
      if(disposed||reconnectTimer!==undefined)return
      reconnectTimer=window.setTimeout(()=>{reconnectTimer=undefined;connect()},1000)
    }
    function connect(){
      if(disposed||!enabled||socket?.readyState===WebSocket.OPEN||socket?.readyState===WebSocket.CONNECTING)return
      const url=new URL('/api/runtime/session',window.location.href)
      url.protocol=window.location.protocol==='https:'?'wss:':'ws:'
      socket=new WebSocket(url)
      socket.addEventListener('open',()=>{
        if(disposed){socket?.close();return}
        heartbeatTimer=window.setInterval(()=>{
          if(socket?.readyState===WebSocket.OPEN)socket.send('ping')
        },10000)
      })
      socket.addEventListener('close',()=>{
        if(heartbeatTimer!==undefined)window.clearInterval(heartbeatTimer)
        heartbeatTimer=undefined
        socket=undefined
        scheduleReconnect()
      })
      socket.addEventListener('error',()=>socket?.close())
    }

    fetch('/api/runtime/status',{cache:'no-store'})
      .then(response=>response.ok?response.json():null)
      .then((status:RuntimeStatus|null)=>{if(!disposed&&status?.local_runtime&&status.automatic_shutdown){enabled=true;connect()}})
      .catch(()=>{/* The normal API error UI remains responsible for connection failures. */})

    function reconnectWhenVisible(){if(document.visibilityState==='visible'&&socket?.readyState!==WebSocket.OPEN)connect()}
    document.addEventListener('visibilitychange',reconnectWhenVisible)
    window.addEventListener('online',connect)
    return ()=>{
      disposed=true
      document.removeEventListener('visibilitychange',reconnectWhenVisible)
      window.removeEventListener('online',connect)
      clearTimers()
      socket?.close(1000,'Dashboard closed')
      socket=undefined
    }
  },[])
}
